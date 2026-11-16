"""One exact CONNECT UNKNOWN; synthetic identities, SQLite and fake transport."""
import asyncio,copy,hashlib,json,sqlite3,sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'legacy_app'),str(Path(__file__).parent)]
import connect_receipt_hold as hold
from test_four_specials_pipeline import pipeline
from test_six_unknown_hold import Bot

@pytest.fixture
def single(tmp_path,monkeypatch):
    from success_receipt_audit import append
    from frozen_direct_receipts import terminal_links
    path=tmp_path/'audit.jsonl';monkeypatch.setenv('RAINBOW_RECEIPT_AUDIT_PATH',str(path))
    c=sqlite3.connect(tmp_path/'proof.sqlite3');c.row_factory=sqlite3.Row
    c.executescript('''CREATE TABLE orders(id INTEGER PRIMARY KEY,order_number,business_order_number,source_chat_id,source_message_id,status);
    CREATE TABLE telegram_execution_commits(namespace,chat_id,message_id,order_id);
    CREATE TABLE telegram_order_jobs(namespace,chat_id,message_id,fingerprint,state,reason,attempts,received_at,started_at,finished_at,lease_until,lease_token,payload);
    CREATE TABLE telegram_execution_receipts(event_key,chat_id,source_message_id,status,attempts);''')
    oid=18274;number='SYNTHETIC18274';public='PUBLIC18274';chat='-1009999999911';msg=123;ns='shared';source=hold.hashed(f'{ns}:{chat}:{msg}')
    c.execute("INSERT INTO orders VALUES(?,?,?,?,?,'pending')",(oid,number,public,chat,msg));c.execute('INSERT INTO telegram_execution_commits VALUES(?,?,?,?)',(ns,chat,msg,oid))
    c.execute("INSERT INTO telegram_order_jobs VALUES(?,?,?,'synthetic','COMPLETED','ORDER_COMMITTED',1,1,2,3,NULL,NULL,'SYNTHETIC-CIPHERTEXT')",(ns,chat,msg));c.commit()
    record=dict(attempt_id='synthetic18274',task_id=source[:16],order_numbers=[number],method='send_message',delivery_path='DIRECT_BOT',outbox_event_key=None)
    append(dict(record,status='SENDING'));append(dict(record,status='UNKNOWN',message_id=None,ack_at=None,result_kind='AMBIGUOUS',transport_phase='CONNECT',exception_kind='TimedOut',cause_kind='ConnectTimeout'))
    chain=[json.loads(x) for x in path.read_text().splitlines()]
    pin=dict(order_id=oid,source_identity_sha256=source,source_pair_sha256=hold.hashed(f'{chat}:{msg}'),number_sha256=hold.hashed(number),public_number_sha256=hold.hashed(public),attempt_ref=hold.hashed(record['attempt_id']),original_sending_sha256=hold.digest(chain[0]),unknown_row_sha256=hold.digest(chain[-1]),terminal_links_sha256=hold.digest(terminal_links(c,chain[-1])),job_metadata_sha256=hold.digest(hold.job_metadata(c,(ns,chat,msg))),_scope='CONNECT18274')
    monkeypatch.setattr(hold,'PIN',pin);monkeypatch.setattr(hold,'APPROVED_ATTEMPT',pin['attempt_ref'])
    yield c,path,dict(namespace=ns,chat_id=chat,message_id=msg,number=number,public=public,order_id=oid),chain
    c.close()


def test_independent_review_is_read_only_not_delivery_success(single):
    c,path,case,chain=single;before=path.read_bytes();changes=c.total_changes;c.execute('PRAGMA query_only=ON')
    unrelated=dict(chain[-1],attempt_id='OTHER',task_id='other',order_numbers=['OTHER'])
    remaining,accepted=hold.partition_verified([chain[-1],unrelated],c)
    assert remaining==[unrelated] and accepted==['synthetic18274']
    assert c.total_changes==changes and path.read_bytes()==before and chain[-1]['status']=='UNKNOWN' and chain[-1]['message_id'] is None

@pytest.mark.parametrize('fault',['missing','state','phase','chain','job','lease','ciphertext','public','commit','recovery','new_attempt'])
def test_history_or_source_drift_is_not_admitted(single,fault):
    from success_receipt_audit import append
    c,path,case,chain=single;unresolved=[copy.deepcopy(chain[-1])]
    if fault=='missing':unresolved=[]
    elif fault=='state':unresolved[0]['status']='SENDING'
    elif fault=='phase':unresolved[0]['transport_phase']='READ'
    elif fault=='chain':path.write_text(json.dumps(chain[-1])+'\n')
    elif fault=='job':c.execute("UPDATE telegram_order_jobs SET state='RECEIVED'")
    elif fault=='lease':c.execute("UPDATE telegram_order_jobs SET lease_until=999,lease_token='active'")
    elif fault=='ciphertext':c.execute("UPDATE telegram_order_jobs SET payload='changed'")
    elif fault=='public':c.execute("UPDATE orders SET business_order_number='changed'")
    elif fault=='commit':c.execute('DELETE FROM telegram_execution_commits')
    elif fault=='recovery':c.execute("INSERT INTO telegram_execution_receipts VALUES('injected',?,?,'pending',0)",(case['chat_id'],case['message_id']))
    elif fault=='new_attempt':append(dict(chain[0],attempt_id='new-replay'))
    before=path.read_bytes();changes=c.total_changes
    with pytest.raises(ValueError):hold.partition_verified(unresolved,c)
    assert c.total_changes==changes and path.read_bytes()==before

@pytest.mark.parametrize('identity',['internal','public','id','context'])
def test_direct_wrapper_blocked_before_attempt(single,identity,caplog):
    from success_receipt_audit import ReceiptSender
    from six_unknown_receipt_hold import ReceiptReplayHeld
    from execution_safety import execution
    c,path,case,_=single;before=path.read_bytes();bot=Bot()
    async def run():
        facts={'order_number':case['number']} if identity=='internal' else {'order_number':case['public']} if identity=='public' else {'order_number':'OTHER','id':18274} if identity=='id' else {'order_number':'OTHER'}
        if identity=='context':
            with execution(case):
                with pytest.raises(ReceiptReplayHeld):await ReceiptSender(bot,facts).send_message(chat_id=case['chat_id'],text='NEVER')
        else:
            with pytest.raises(ReceiptReplayHeld):await ReceiptSender(bot,facts).send_message(chat_id=case['chat_id'],text='NEVER')
    asyncio.run(run());assert not bot.calls and path.read_bytes()==before
    assert 'CONNECT18274_ORIGINAL_ORDER_RECEIPT_HELD' in caplog.text and 'attempt_created=false' in caplog.text

@pytest.mark.parametrize('mode',['reply','reply_parameters','reply_json','context_without_reply'])
def test_direct_sdk_success_receipt_identity_blocked(single,mode):
    from telegram_delivery import TelegramDeliveryLimiter
    from six_unknown_receipt_hold import ReceiptReplayHeld
    from execution_safety import execution
    c,path,case,_=single;before=path.read_bytes();calls=[]
    async def send(*a,**kw):calls.append(1)
    async def run():
        limiter=TelegramDeliveryLimiter(group_interval=0,global_interval=0)
        data=dict(chat_id=case['chat_id'],text='HELD')
        if mode=='reply':data['reply_to_message_id']=case['message_id']
        elif mode=='reply_parameters':data['reply_parameters']={'message_id':case['message_id']}
        elif mode=='reply_json':data['reply_parameters']=json.dumps({'message_id':case['message_id']})
        if mode=='context_without_reply':
            with execution(case):
                with pytest.raises(ReceiptReplayHeld):await limiter.process_request(send,(),{},'sendMessage',data,{'order_receipt':True})
        else:
            with pytest.raises(ReceiptReplayHeld):await limiter.process_request(send,(),{},'sendMessage',data,{'order_receipt':True})
        assert limiter.pending==0
    asyncio.run(run());assert not calls and path.read_bytes()==before


def test_pending_injection_and_concurrent_dispatch_not_claimed(single,pipeline):
    from sqlalchemy import text
    from execution_safety import deliver,_queue_receipt
    c,path,case,_=single;p=pipeline
    import os
    os.environ['RAINBOW_RECEIPT_AUDIT_PATH']=str(path);before=path.read_bytes()
    with p.db.engine.begin() as con:
        for i in range(2):con.execute(text("INSERT INTO telegram_execution_receipts(event_key,chat_id,source_message_id,body,status,created_at) VALUES(:key,:chat,:msg,'HELD','pending',1)"),dict(key='final:'+hold.scope()['source_identity_sha256']+':'+str(i),chat=case['chat_id'],msg=case['message_id']))
        con.execute(text("INSERT INTO telegram_execution_receipts(event_key,chat_id,source_message_id,body,status,created_at) VALUES('final:legal:0','-999',999,'LEGAL','pending',2)"))
    bot=Bot()
    async def run():await asyncio.gather(*(deliver(p.db,bot,limit=1) for _ in range(4)))
    asyncio.run(run());assert len(bot.calls)==1 and bot.calls[0]['text']=='LEGAL'
    with p.db.engine.connect() as con:assert con.execute(text("SELECT status,attempts FROM telegram_execution_receipts WHERE event_key!='final:legal:0'")).all()==[('pending',0),('pending',0)]
    with p.db.SessionLocal() as session:assert _queue_receipt(session,case,(case['namespace'],case['chat_id'],case['message_id']),[18274],'TEST') is False
    assert path.read_bytes()==before


def test_parallel_direct_invocations_never_create_attempt(single):
    from success_receipt_audit import ReceiptSender
    from six_unknown_receipt_hold import ReceiptReplayHeld
    _,path,case,_=single;before=path.read_bytes();bot=Bot()
    async def run():
        results=await asyncio.gather(*(ReceiptSender(bot,{'order_number':case['number']}).send_message(chat_id=case['chat_id'],text='HELD') for _ in range(16)),return_exceptions=True)
        assert all(isinstance(r,ReceiptReplayHeld) for r in results)
    asyncio.run(run());assert not bot.calls and path.read_bytes()==before


def test_normal_new_order_and_same_order_new_notification_unaffected(single,pipeline):
    from telegram_delivery import TelegramDeliveryLimiter
    from account_notifications import dispatch
    from success_receipt_audit import ReceiptSender
    c,path,case,chain=single;before=path.read_bytes();calls=[]
    async def network(*a,**k):calls.append(1);return True
    async def run():
        limiter=TelegramDeliveryLimiter(group_interval=0,global_interval=0)
        # A distinct notification is not an order-success replay, even with the same Reply.
        await limiter.process_request(network,(),{},'sendMessage',dict(chat_id=case['chat_id'],reply_to_message_id=case['message_id'],text='NEW ACCOUNT NOTICE'),{})
        async def send(*a,**k):calls.append(1);return True,'ok'
        result=await dispatch(pipeline.db,'new-account-notice:18274',pipeline.user.id,'manual_bill','NEW LEGAL NOTICE',send)
        assert result['sent']
    asyncio.run(run());assert len(calls)==2 and path.read_bytes()==before
    bot=Bot();asyncio.run(ReceiptSender(bot,dict(order_number='NEW-ORDER')).send_message(chat_id=-999,text='NEW SUCCESS'))
    assert len(bot.calls)==1
    assert hold.partition_verified([chain[-1]],c)[1]==['synthetic18274']


def test_business_status_update_does_not_change_send_identity(single):
    c,_,_,chain=single;c.execute("UPDATE orders SET status='confirmed'")
    assert hold.partition_verified([chain[-1]],c)[1]==['synthetic18274']
