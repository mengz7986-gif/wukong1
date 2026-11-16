"""Seven exact recovery outcomes; synthetic SQLite and forbidden transport."""
import asyncio,hashlib,json,sys,copy
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.orm import sessionmaker
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'legacy_app')]
import recovery_timeout_hold as hold
import p0_recovery_protection as p0

@pytest.fixture
def seven(tmp_path,monkeypatch):
    engine=create_engine('sqlite:///'+str(tmp_path/'isolated.sqlite3'))
    schema='''CREATE TABLE telegram_order_jobs(namespace TEXT,chat_id TEXT,message_id INTEGER,sender_id TEXT,state TEXT,received_at REAL,source_date REAL,started_at REAL,finished_at REAL,lease_until REAL,lease_token TEXT,attempts INTEGER,fingerprint TEXT,draft_token TEXT);
    CREATE TABLE orders(id INTEGER,source_chat_id TEXT,source_message_id INTEGER);
    CREATE TABLE telegram_execution_commits(namespace TEXT,chat_id TEXT,message_id INTEGER,order_id INTEGER);
    CREATE TABLE telegram_order_drafts(namespace TEXT,chat_id TEXT,message_id INTEGER,token TEXT,status TEXT,payload TEXT);
    CREATE TABLE telegram_execution_receipts(event_key TEXT PRIMARY KEY,chat_id TEXT,source_message_id INTEGER,body TEXT,edit_message_id INTEGER,status TEXT,attempts INTEGER DEFAULT 0,created_at REAL,delivered_message_id INTEGER,error_code TEXT);
    CREATE TABLE telegram_source_fences(namespace TEXT,chat_id TEXT,message_id INTEGER,reason TEXT,created_at REAL);'''
    with engine.begin() as c:
        for sql in schema.split(';'):
            if sql.strip():c.execute(text(sql))
        for n in range(9):
            c.execute(text("INSERT INTO telegram_order_jobs VALUES('shared','-123',:n,'123',:s,100,100,100,NULL,:lease,:token,1,'old',NULL)"),dict(n=n+1,s='PROCESSING' if n<7 else 'RECEIVED',lease=120 if n<7 else None,token='frozen' if n<7 else None))
        for n in range(29):
            c.execute(text("INSERT INTO telegram_execution_receipts VALUES(:key,'-123',:n,'old',NULL,'unknown',1,100,NULL,'delivery_unknown')"),dict(key=f'old-{n:02}',n=n+100))
        pins=[];cases=[]
        for n in range(7):
            chat=str(-1000000001000-n);msg=1000+n;identity=hold.hashed(f'shared:{chat}:{msg}');key='final:'+identity+':0';args=dict(chat=chat,msg=msg,key=key,token='draft-'+str(n))
            c.execute(text("INSERT INTO telegram_order_jobs VALUES('shared',:chat,:msg,'999','TIMED_OUT',200,200,200,220,NULL,NULL,1,'new',:token)"),args)
            c.execute(text("INSERT INTO telegram_execution_receipts VALUES(:key,:chat,:msg,'SYNTHETIC TIMEOUT',NULL,'unknown',1,220,NULL,'TimedOut')"),args)
            c.execute(text("INSERT INTO telegram_order_drafts VALUES('shared',:chat,:msg,:token,'timeout_failed','{}')"),args)
            c.execute(text("INSERT INTO telegram_source_fences VALUES('shared',:chat,:msg,'ORDER_TIMEOUT',220)"),args)
            row=lambda sql:[dict(x) for x in c.execute(text(sql),args).mappings().all()]
            pin=dict(event_sha256=hold.hashed(key),source_identity_sha256=identity,source_pair_sha256=hold.hashed(f'{chat}:{msg}'),edit_pair_sha256=None,receipt_sha256=hold.digest(row('SELECT * FROM telegram_execution_receipts WHERE event_key=:key')[0]),job_sha256=hold.digest(row('SELECT * FROM telegram_order_jobs WHERE chat_id=:chat AND message_id=:msg')[0]),drafts_sha256=hold.digest(row('SELECT * FROM telegram_order_drafts WHERE chat_id=:chat AND message_id=:msg ORDER BY token')),fences_sha256=hold.digest(row('SELECT * FROM telegram_source_fences WHERE chat_id=:chat AND message_id=:msg')))
            pins.append(pin);cases.append(dict(args,namespace='shared',chat_id=chat,message_id=msg))
        oldrows=unresolved(c)[:]
        oldrows=[r for r in oldrows if r['event_key'].startswith('old-')]
        entries=[]
        for row in c.execute(text('SELECT * FROM telegram_order_jobs WHERE received_at=100')).mappings():
            pin=p0.job_pin(row);pin.update(commit_order_ids=[],source_order_ids=[],draft_token_sha256=[]);entries.append(pin)
    policy=dict(version=1,incident=p0.INCIDENT,bot_id=p0.BOT_ID,cutoff_received_at=123,entries=entries,frozen_execution_receipts_sha256=hashlib.sha256(json.dumps(oldrows,sort_keys=True).encode()).hexdigest())
    path=tmp_path/'p0.json';raw=json.dumps(policy).encode();path.write_bytes(raw);path.chmod(0o600)
    monkeypatch.setattr(p0,'PINNED_POLICY_SHA256',hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(hold,'APPROVED_EVENTS',tuple(p['event_sha256'] for p in pins));monkeypatch.setattr(hold,'SCOPE',tuple(pins))
    db=SimpleNamespace(engine=engine,SessionLocal=sessionmaker(bind=engine))
    yield db,cases,path
    engine.dispose()

def unresolved(c):
    return [dict(r) for r in c.execute(text("SELECT event_key,chat_id,source_message_id,edit_message_id,status,attempts,created_at,delivered_message_id,error_code FROM telegram_execution_receipts WHERE status IN ('sending','unknown') ORDER BY event_key")).mappings()]

def snapshot(db):
    with db.engine.connect() as c:
        return {t:[tuple(r) for r in c.execute(text('SELECT * FROM '+t+' ORDER BY rowid'))] for t in ['telegram_execution_receipts','telegram_order_jobs','telegram_order_drafts','telegram_source_fences','orders','telegram_execution_commits']}

def test_original_policy_and_new_review_both_pass_without_writes(seven):
    db,_,path=seven;before=snapshot(db);policy=p0.Protection(path);policy.verify_before_workers(db)
    assert policy.verified and snapshot(db)==before
    with db.engine.connect() as c:
        rest,accepted=hold.partition_verified(unresolved(c),c)
        assert len(rest)==29 and len(accepted)==7

@pytest.mark.parametrize('fault',['missing','state','attempt','ack','body','job','lease','fence','draft','order','commit','duplicate','extra_unknown','old_row'])
def test_each_evidence_drift_fails_closed(seven,fault):
    db,cases,path=seven;case=cases[0]
    statements={
      'missing':'DELETE FROM telegram_execution_receipts WHERE event_key=:key',
      'state':"UPDATE telegram_execution_receipts SET status='pending' WHERE event_key=:key",
      'attempt':'UPDATE telegram_execution_receipts SET attempts=2 WHERE event_key=:key',
      'ack':'UPDATE telegram_execution_receipts SET delivered_message_id=999 WHERE event_key=:key',
      'body':"UPDATE telegram_execution_receipts SET body='changed' WHERE event_key=:key",
      'job':"UPDATE telegram_order_jobs SET state='RECEIVED' WHERE chat_id=:chat",
      'lease':'UPDATE telegram_order_jobs SET lease_until=999,lease_token=\'active\' WHERE chat_id=:chat',
      'fence':'DELETE FROM telegram_source_fences WHERE chat_id=:chat',
      'draft':"UPDATE telegram_order_drafts SET status='pending' WHERE chat_id=:chat",
      'order':"INSERT INTO orders VALUES(99,:chat,:msg)",
      'commit':"INSERT INTO telegram_execution_commits VALUES('shared',:chat,:msg,99)",
      'duplicate':"INSERT INTO telegram_execution_receipts VALUES('injected',:chat,:msg,'new',NULL,'pending',0,300,NULL,NULL)",
      'extra_unknown':"INSERT INTO telegram_execution_receipts VALUES('unapproved','-999',999,'new',NULL,'unknown',1,300,NULL,'TimedOut')",
      'old_row':"UPDATE telegram_execution_receipts SET attempts=2 WHERE event_key='old-00'"}
    with db.engine.begin() as c:c.execute(text(statements[fault]),case)
    before=snapshot(db)
    with pytest.raises(p0.ProtectionError):p0.Protection(path).verify_before_workers(db)
    assert snapshot(db)==before

@pytest.mark.parametrize('i',range(7))
def test_untagged_direct_transport_cannot_replay(seven,i,caplog):
    from telegram_delivery import TelegramDeliveryLimiter
    from six_unknown_receipt_hold import ReceiptReplayHeld
    db,cases,_=seven;case=cases[i];before=snapshot(db);calls=[]
    async def forbidden(*a,**k):calls.append(1);raise AssertionError('NETWORK_FORBIDDEN')
    async def run():
        limiter=TelegramDeliveryLimiter()
        for target in [dict(reply_to_message_id=case['msg']),dict(reply_parameters={'message_id':case['msg']}),dict(reply_parameters=json.dumps({'message_id':case['msg']}))]:
            with pytest.raises(ReceiptReplayHeld):await limiter.process_request(forbidden,(),{},'sendMessage',dict(chat_id=case['chat'],text='SYNTHETIC',**target),None)
        assert limiter.pending==0
    asyncio.run(run());assert not calls and snapshot(db)==before
    assert 'RECOVERY7_ORIGINAL_FAILURE_RECEIPT_HELD' in caplog.text and 'attempt_created=false' in caplog.text

@pytest.mark.parametrize('i',range(7))
def test_direct_success_wrapper_cannot_bypass_fenced_source(seven,i,tmp_path,monkeypatch):
    from success_receipt_audit import ReceiptSender
    from execution_safety import execution
    from six_unknown_receipt_hold import ReceiptReplayHeld
    db,cases,_=seven;path=tmp_path/'audit.jsonl';monkeypatch.setenv('RAINBOW_RECEIPT_AUDIT_PATH',str(path));calls=[]
    class Bot:
        async def send_message(self,**kw):calls.append(kw)
    async def run():
        with execution(cases[i]):
            with pytest.raises(ReceiptReplayHeld):await ReceiptSender(Bot(),dict(order_number='UNRELATED')).send_message(chat_id=cases[i]['chat'],text='NEVER')
    asyncio.run(run());assert not calls and not path.exists()


def test_pending_injection_concurrent_dispatch_is_blocked_without_starvation(seven):
    from execution_safety import deliver,recover_dispatches,_queue_receipt
    db,cases,_=seven
    with db.engine.begin() as c:
        c.execute(text("UPDATE telegram_execution_receipts SET status='pending',attempts=0 WHERE event_key LIKE 'final:%'"))
        for i,case in enumerate(cases):
            c.execute(text("INSERT INTO telegram_execution_receipts VALUES(:key,:chat,:msg,'EXTRA',NULL,'pending',0,221,NULL,NULL)"),dict(case,key='final:'+hold.hashed(f"shared:{case['chat']}:{case['msg']}")+':1'))
        c.execute(text("INSERT INTO telegram_execution_receipts VALUES('legal-new','-999',999,'NEW',NULL,'pending',0,222,NULL,NULL)"))
    before=snapshot(db);calls=[]
    class Bot:
        async def send_message(self,**kw):calls.append(kw);await asyncio.sleep(0);return SimpleNamespace(message_id=123)
    async def run():await asyncio.gather(*(deliver(db,Bot(),limit=4) for _ in range(4)))
    asyncio.run(run())
    assert len(calls)==1 and calls[0]['reply_to_message_id']==999
    after=snapshot(db)
    assert after['telegram_execution_receipts'][:-1]==before['telegram_execution_receipts'][:-1]
    assert all(after[t]==before[t] for t in before if t!='telegram_execution_receipts')
    assert recover_dispatches(db)['automatically_rearmed']==0
    with db.SessionLocal() as s:
        for case in cases:assert _queue_receipt(s,case,('shared',case['chat'],case['msg']),[],'ORDER_TIMEOUT') is False
    assert snapshot(db)==after


def test_legal_new_messages_and_callbacks_unaffected(seven):
    from telegram_delivery import TelegramDeliveryLimiter
    db,cases,_=seven;calls=[];case=cases[0]
    async def transport(*a,**kw):calls.append(1);return True
    async def run():
        limiter=TelegramDeliveryLimiter(group_interval=0,private_interval=0,global_interval=0)
        assert await limiter.process_request(transport,(),{},'sendMessage',dict(chat_id=case['chat'],reply_to_message_id=99999,text='NEW'),None)
        assert await limiter.process_request(transport,(),{},'answerCallbackQuery',dict(callback_query_id='LEGAL'),None)
        assert await limiter.process_request(transport,(),{},'editMessageText',dict(chat_id=case['chat'],message_id=99999,text='NEW'),None)
    asyncio.run(run());assert len(calls)==3


@pytest.mark.parametrize('i',range(7))
def test_direct_transport_context_without_reply_is_still_held(seven,i):
    from execution_safety import execution
    from telegram_delivery import TelegramDeliveryLimiter
    from six_unknown_receipt_hold import ReceiptReplayHeld
    _,cases,_=seven;calls=[]
    async def transport(*a,**k):calls.append(1)
    async def run():
        with execution(cases[i]):
            with pytest.raises(ReceiptReplayHeld):await TelegramDeliveryLimiter().process_request(transport,(),{},'sendMessage',dict(chat_id=cases[i]['chat'],text='NEVER'),None)
    asyncio.run(run());assert not calls
