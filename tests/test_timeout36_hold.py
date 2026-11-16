"""Exact 36 review and hard replay fences, synthetic identities only."""
import asyncio,hashlib,json,sqlite3,sys,time,copy
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'legacy_app'),str(Path(__file__).parent)]
import timeout_receipt_hold as hold
from test_six_unknown_hold import Bot
from test_four_specials_pipeline import pipeline

@pytest.fixture
def held36(tmp_path,monkeypatch):
    from success_receipt_audit import append
    from frozen_direct_receipts import terminal_links
    path=tmp_path/'hold36-audit.jsonl';monkeypatch.setenv('RAINBOW_RECEIPT_AUDIT_PATH',str(path))
    c=sqlite3.connect(tmp_path/'review.db');c.row_factory=sqlite3.Row
    c.executescript('''CREATE TABLE orders(id INTEGER PRIMARY KEY,order_number,business_order_number,source_chat_id,source_message_id,status);
    CREATE TABLE telegram_execution_commits(namespace,chat_id,message_id,order_id);
    CREATE TABLE telegram_order_jobs(namespace,chat_id,message_id,fingerprint,state,reason,attempts,received_at,started_at,finished_at,lease_until,lease_token,payload);
    CREATE TABLE telegram_execution_receipts(event_key,chat_id,source_message_id,status,attempts);''')
    pins=[];cases=[];unknown=[]
    for i in range(36):
        oid=8000+i;number=f'SYNTHETIC-{i}';public=f'PUBLIC-{i}';chat=str(-1001000000000-i);message=100+i;namespace='shared'
        full=hashlib.sha256(f'{namespace}:{chat}:{message}'.encode()).hexdigest();payload=f'ENCRYPTED-SYNTHETIC-{i}'
        c.execute("INSERT INTO orders VALUES(?,?,?,?,?,'pending')",(oid,number,public,chat,message))
        c.execute('INSERT INTO telegram_execution_commits VALUES(?,?,?,?)',(namespace,chat,message,oid))
        c.execute("INSERT INTO telegram_order_jobs VALUES(?,?,?,?,'COMPLETED','ORDER_COMMITTED',1,1,2,3,NULL,NULL,?)",(namespace,chat,message,'fingerprint-'+str(i),payload))
        base=dict(attempt_id=f'hold36-{i}',task_id=full[:16],order_numbers=[number],method='send_message',delivery_path='DIRECT_BOT',outbox_event_key=None)
        append(dict(base,status='SENDING'));append(dict(base,status='UNKNOWN',message_id=None,result_kind='AMBIGUOUS'))
        chain=[json.loads(x) for x in path.read_text().splitlines()][-2:];unknown.append(chain[-1])
        pin=dict(_scope='TIMEOUT36',order_id=oid,source_identity_sha256=full,source_pair_sha256=hashlib.sha256(f'{chat}:{message}'.encode()).hexdigest(),number_sha256=hashlib.sha256(number.encode()).hexdigest(),public_number_sha256=hashlib.sha256(public.encode()).hexdigest(),attempt_ref=hashlib.sha256(base['attempt_id'].encode()).hexdigest(),original_sending_sha256=hold.digest(chain[0]),unknown_row_sha256=hold.digest(chain[-1]),terminal_links_sha256=hold.digest(terminal_links(c,chain[-1])),job_metadata_sha256=hold.digest(hold.job_metadata(c,(namespace,chat,message))))
        pins.append(pin);cases.append(dict(order_id=oid,number=number,public=public,namespace=namespace,chat_id=chat,message_id=message))
    c.commit();monkeypatch.setattr(hold,'APPROVED_ORDER_IDS',tuple(x['order_id'] for x in pins));monkeypatch.setattr(hold,'SCOPE',tuple(pins))
    yield c,path,cases,unknown
    c.close()

@pytest.mark.parametrize('index',range(36))
def test_each_historical_identity_blocks_direct_before_attempt(held36,index,caplog):
    from success_receipt_audit import ReceiptSender
    from execution_safety import execution
    from six_unknown_receipt_hold import ReceiptReplayHeld
    c,path,cases,_=held36;case=cases[index];before=path.read_bytes();bot=Bot()
    async def run():
        for facts in [dict(order_number=case['number']),dict(order_number=case['public']),dict(order_number='OTHER',id=case['order_id'])]:
            with pytest.raises(ReceiptReplayHeld):await ReceiptSender(bot,facts).send_message(chat_id=case['chat_id'],text='NEVER')
        with execution(case):
            with pytest.raises(ReceiptReplayHeld):await ReceiptSender(bot,dict(order_number='OTHER')).edit_message_text(chat_id=case['chat_id'],message_id=1,text='NEVER')
    asyncio.run(run());assert not bot.calls and path.read_bytes()==before
    assert 'TIMEOUT36_ORIGINAL_ORDER_RECEIPT_HELD' in caplog.text and 'attempt_created=false' in caplog.text


def test_36_independent_review_does_not_write_or_claim_ack(held36):
    c,path,_,unknown=held36;before=path.read_bytes();changes=c.total_changes
    c.execute('PRAGMA query_only=ON')
    remaining,accepted=hold.partition_verified(unknown,c)
    assert remaining==[] and len(accepted)==36 and c.total_changes==changes and path.read_bytes()==before
    assert all(x['status']=='UNKNOWN' and x['message_id'] is None for x in unknown)

@pytest.mark.parametrize('fault',['missing','status','chain','job','lease','ciphertext','public','commit','recovery'])
def test_independent_review_fails_closed_on_drift(held36,fault):
    c,path,cases,unknown=held36;data=copy.deepcopy(unknown)
    if fault=='missing':data.pop()
    elif fault=='status':data[0]['status']='SENDING'
    elif fault=='chain':
        lines=path.read_text().splitlines();lines.pop(0);path.write_text('\n'.join(lines)+'\n')
    elif fault=='job':c.execute("UPDATE telegram_order_jobs SET state='RECEIVED' WHERE message_id=100")
    elif fault=='lease':c.execute("UPDATE telegram_order_jobs SET lease_until=100,lease_token='NEW' WHERE message_id=100")
    elif fault=='ciphertext':c.execute("UPDATE telegram_order_jobs SET payload='OTHER' WHERE message_id=100")
    elif fault=='public':c.execute("UPDATE orders SET business_order_number='OTHER' WHERE id=8000")
    elif fault=='commit':c.execute('DELETE FROM telegram_execution_commits WHERE order_id=8000')
    elif fault=='recovery':c.execute("INSERT INTO telegram_execution_receipts VALUES('injected',?,100,'pending',0)",(cases[0]['chat_id'],))
    with pytest.raises(ValueError):hold.partition_verified(data,c)


def test_business_status_changes_are_not_part_of_send_hold(held36):
    c,path,_,unknown=held36;c.execute("UPDATE orders SET status='shipped'")
    assert len(hold.partition_verified(unknown,c)[1])==36


def test_same_source_new_attempt_is_not_an_approval_exception(held36):
    from success_receipt_audit import append
    c,path,_,unknown=held36;row=dict(unknown[0],attempt_id='NEW-REPLAY',status='SENDING');append(row)
    with pytest.raises(ValueError,match='NEW_ATTEMPT_FOR_SOURCE'):hold.partition_verified(unknown,c)


def test_injected_pending_36_never_claimed_normal_new_receipt_not_starved(held36,pipeline):
    from sqlalchemy import text
    from execution_safety import deliver,_queue_receipt
    c,path,cases,_=held36;p=pipeline;before=path.read_bytes()
    # pipeline changes environment, explicitly restore this test's audit path.
    import os
    os.environ['RAINBOW_RECEIPT_AUDIT_PATH']=str(path)
    with p.db.engine.begin() as conn:
        for case,pin in zip(cases,hold.scope()):
            conn.execute(text("INSERT INTO telegram_execution_receipts(event_key,chat_id,source_message_id,body,status,created_at) VALUES(:key,:chat,:message,'HELD','pending',1)"),dict(key='final:'+pin['source_identity_sha256']+':0',chat=case['chat_id'],message=case['message_id']))
        conn.execute(text("INSERT INTO telegram_execution_receipts(event_key,chat_id,source_message_id,body,status,created_at) VALUES('final:new:0','-999',999,'NEW','pending',2)"))
    bot=Bot()
    async def run():await asyncio.gather(*(deliver(p.db,bot,limit=1) for _ in range(4)))
    asyncio.run(run());assert len(bot.calls)==1 and bot.calls[0]['text']=='NEW'
    with p.db.engine.connect() as conn:
        rows=conn.execute(text("SELECT status,attempts FROM telegram_execution_receipts WHERE event_key!='final:new:0'" )).all();assert rows==[('pending',0)]*36
    with p.db.SessionLocal() as session:
        for case in cases:assert _queue_receipt(session,case,(case['namespace'],case['chat_id'],case['message_id']),[case['order_id']],'TEST') is False
    assert path.read_bytes()==before


def test_transport_and_concurrent_direct_block_but_legal_new_notice_passes(held36):
    from telegram_delivery import TelegramDeliveryLimiter
    from success_receipt_audit import ReceiptSender
    from six_unknown_receipt_hold import ReceiptReplayHeld
    c,path,cases,_=held36;before=path.read_bytes();calls=[];limiter=TelegramDeliveryLimiter(group_interval=0,global_interval=0)
    async def network(*a,**kw):calls.append(kw);return True
    async def run():
        for case in cases:
            data=dict(chat_id=case['chat_id'],reply_to_message_id=case['message_id'],text='HELD')
            with pytest.raises(ReceiptReplayHeld):await limiter.process_request(network,(),{},'sendMessage',data,{'order_receipt':True})
        bot=Bot()
        results=await asyncio.gather(*(ReceiptSender(bot,dict(order_number=cases[0]['number'])).send_message(chat_id=cases[0]['chat_id'],text='HELD') for _ in range(8)),return_exceptions=True)
        assert all(isinstance(x,ReceiptReplayHeld) for x in results) and not bot.calls
        # The same source Reply on a distinct notification channel is legal.
        await limiter.process_request(network,(),{},'sendMessage',dict(chat_id=cases[0]['chat_id'],reply_to_message_id=cases[0]['message_id'],text='NEW ACCOUNT NOTICE'),{})
    asyncio.run(run());assert len(calls)==1 and path.read_bytes()==before


def test_same_order_legal_account_notification_uses_own_identity(held36,pipeline):
    from account_notifications import dispatch
    c,path,cases,_=held36;p=pipeline;calls=[];before=path.read_bytes()
    import os
    os.environ['RAINBOW_RECEIPT_AUDIT_PATH']=str(path)
    async def send(chat,message,**kw):calls.append((chat,message));return True,'ok'
    result=asyncio.run(dispatch(p.db,'new-account-notice:order:'+str(cases[0]['order_id']),p.user.id,'manual_bill','NEW LEGAL NOTICE',send))
    assert result['sent'] and len(calls)==1 and path.read_bytes()==before
