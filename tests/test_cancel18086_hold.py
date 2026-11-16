"""Independent18086 plus original15920, synthetic records and fake transports."""
import asyncio,json,sqlite3,sys
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'legacy_app')]
import cancel_notice_hold as hold
import cancel_notice_worker as worker
from telegram_delivery_sync import send_message

@pytest.fixture
def pair(tmp_path,monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:(_ for _ in ()).throw(AssertionError('REAL_NETWORK_FORBIDDEN')))
    path=tmp_path/'isolated.sqlite3';c=sqlite3.connect(path);c.row_factory=sqlite3.Row
    c.executescript('''CREATE TABLE telegram_notification_events(event_key TEXT PRIMARY KEY,user_id INTEGER,chat_id TEXT,kind TEXT,payload TEXT,status TEXT,attempts INTEGER,created_at TEXT,sent_at TEXT,last_error TEXT);
    CREATE TABLE order_cancellation_facts(event_id TEXT,order_id INTEGER,cancelled_at TEXT,reason TEXT);
    CREATE TABLE orders(id INTEGER,user_id INTEGER,order_number TEXT,source_chat_id TEXT,source_message_id INTEGER,status TEXT);
    CREATE TABLE users(id INTEGER,telegram_id TEXT);''')
    c.execute("INSERT INTO users VALUES(1,'123')")
    for n in [1,2]:
        c.execute("INSERT INTO orders VALUES(?,1,?,'-123',?,'cancelled')",(n,'TEST'+str(n),80+n))
        c.execute('INSERT INTO order_cancellation_facts VALUES(?,?,?,?)',('synthetic-'+str(n),n,'2026-11-03T09:19:45+00:00','SYNTHETIC'))
        payload=dict(text='❌ 订单已取消\n订单号：TEST'+str(n),reply_to_message_id=80+n)
        c.execute('INSERT INTO telegram_notification_events VALUES(?,1,?,\'cancelled\',?,\'sending\',1,?,NULL,NULL)',('cancel:synthetic-'+str(n),'-123',json.dumps(payload),'2026-11-03T09:19:45+00:00'))
    c.commit()
    for n in [1,2]:
        row=dict(c.execute('SELECT * FROM telegram_notification_events WHERE event_key=?',('cancel:synthetic-'+str(n),)).fetchone());fact=dict(c.execute('SELECT * FROM order_cancellation_facts WHERE order_id=?',(n,)).fetchone());payload=dict(json.loads(row['payload']),chat_id=row['chat_id'])
        names=('ORDER_ID','EVENT_REF','ROW_SHA256','FACT_SHA256','ARTIFACT_SHA256') if n==1 else ('ORDER_18086','EVENT_18086_REF','ROW_18086_SHA256','FACT_18086_SHA256','ARTIFACT_18086_SHA256')
        for name,value in zip(names,[n,hold.event_ref(row['event_key']),hold.digest(row),hold.digest(fact),hold.artifact(payload)]):monkeypatch.setattr(hold,name,value)
    monkeypatch.setenv('RAINBOW_DEPLOY_DRAIN_PATH',str(tmp_path/'drain.json'))
    yield c,dict(payload),path
    c.close()

def history(c):
    return {t:[tuple(x) for x in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in ['telegram_notification_events','order_cancellation_facts','orders','users']}

def test_both_independent_gates_are_mandatory_and_read_only(pair):
    c,_,_=pair;before=history(c);changes=c.total_changes;c.execute('PRAGMA query_only=ON')
    result=hold.verify(c);assert result['event_ref']==hold.EVENT_REF and result['independent_18086']['event_ref']==hold.EVENT_18086_REF
    assert result['independent_18086']['delivery_outcome']=='UNPROVEN' and c.total_changes==changes and history(c)==before

@pytest.mark.parametrize('fault',['missing','state','attempts','ack','payload','fact','duplicate_fact','order','clone'])
def test_18086_drift_does_not_become_new_exception(pair,fault):
    c,_,_=pair
    sql={
      'missing':"DELETE FROM telegram_notification_events WHERE event_key='cancel:synthetic-2'",
      'state':"UPDATE telegram_notification_events SET status='pending' WHERE event_key='cancel:synthetic-2'",
      'attempts':"UPDATE telegram_notification_events SET attempts=2 WHERE event_key='cancel:synthetic-2'",
      'ack':"UPDATE telegram_notification_events SET sent_at='unproven-success' WHERE event_key='cancel:synthetic-2'",
      'payload':"UPDATE telegram_notification_events SET payload='{}' WHERE event_key='cancel:synthetic-2'",
      'fact':"UPDATE order_cancellation_facts SET reason='changed' WHERE order_id=2",
      'duplicate_fact':"INSERT INTO order_cancellation_facts SELECT 'clone',order_id,cancelled_at,reason FROM order_cancellation_facts WHERE order_id=2",
      'order':"UPDATE orders SET status='pending' WHERE id=2",
      'clone':"INSERT INTO telegram_notification_events SELECT 'cancel:clone',user_id,chat_id,kind,payload,'pending',0,created_at,NULL,NULL FROM telegram_notification_events WHERE event_key='cancel:synthetic-2'"}[fault]
    c.execute(sql);before=history(c)
    with pytest.raises(RuntimeError,match='CANCEL_18086_'):hold.verify(c)
    assert history(c)==before


def test_original15920_change_still_blocks_even_when18086_valid(pair):
    c,_,_=pair;c.execute("UPDATE telegram_notification_events SET attempts=2 WHERE event_key='cancel:synthetic-1'")
    with pytest.raises(RuntimeError,match='CANCEL_15920_'):hold.verify(c)

@pytest.mark.parametrize('status,attempts',[('sending',1),('unknown',1),('pending',1),('pending',0)])
def test_scheduler_and_recovery_never_claim18086(pair,status,attempts):
    c,_,_=pair;c.execute("UPDATE telegram_notification_events SET status=?,attempts=? WHERE event_key='cancel:synthetic-2'",(status,attempts));c.commit();before=history(c)
    assert worker.deliver_one(c,'2026-11-01T00:00:00+00:00',lambda p:pytest.fail('REPLAY'))=='idle'
    assert history(c)==before

@pytest.mark.parametrize('mode',['key','raw','no_reply','changed_text_with_key','notice_context','cancel_context'])
def test_direct_sync_transport_rejected_before_attempt(pair,mode,caplog):
    c,payload,_=pair;payload=dict(payload);before=history(c);kw={};token=None
    from notice_observation import begin,end
    if mode in ['key','changed_text_with_key']:kw['event_key']='cancel:synthetic-2'
    if mode=='changed_text_with_key':payload['text']='CHANGED'
    if mode=='no_reply':payload.pop('reply_to_message_id')
    if mode=='notice_context':token=begin('cancel:synthetic-2');payload['text']='CHANGED'
    if mode=='cancel_context':token=hold.CURRENT_EVENT.set(hold.EVENT_18086_REF);payload['text']='CHANGED'
    try:
        with pytest.raises(hold.CancellationReplayHeld):send_message('SYNTHETIC',payload,state=SimpleNamespace(reserve=lambda *a:pytest.fail('PACING')),transport=lambda *a,**k:pytest.fail('NETWORK'),**kw)
    finally:
        if mode=='notice_context':end(token)
        if mode=='cancel_context':hold.CURRENT_EVENT.reset(token)
    assert history(c)==before and 'CANCEL_18086_ORIGINAL_EVENT_HELD' in caplog.text and 'attempt_created=false' in caplog.text


def test_direct_async_sdk_transport_without_event_key_is_held(pair):
    from telegram_delivery import TelegramDeliveryLimiter
    _,payload,_=pair;calls=[]
    async def forbidden(*a,**k):calls.append(1)
    async def run():
        limiter=TelegramDeliveryLimiter()
        with pytest.raises(hold.CancellationReplayHeld):await limiter.process_request(forbidden,(),{},'sendMessage',payload,None)
        assert limiter.pending==0
    asyncio.run(run());assert not calls


def test_concurrent_rearmed_pending_and_direct_calls_do_not_mutate(pair):
    c,payload,path=pair;c.execute("UPDATE telegram_notification_events SET status='pending',attempts=0 WHERE event_key='cancel:synthetic-2'");c.commit();before=history(c)
    def claim(_):
        with sqlite3.connect(path,timeout=3) as conn:return worker.claim(conn,'2026-11-01T00:00:00+00:00')
    def direct(_):
        try:send_message('SYNTHETIC',payload,transport=lambda *a,**k:pytest.fail('NETWORK'))
        except hold.CancellationReplayHeld:return True
    with ThreadPoolExecutor(max_workers=4) as ex:assert list(ex.map(claim,range(32)))==[None]*32;assert all(ex.map(direct,range(32)))
    assert history(c)==before

@pytest.mark.parametrize('entry',['dispatch','_dispatch_active','drain_pending'])
def test_generic_notice_dispatch_cannot_rearm18086(pair,entry):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import account_notifications as notices
    c,_,path=pair;c.execute("UPDATE telegram_notification_events SET status='pending',attempts=0 WHERE event_key='cancel:synthetic-2'");c.commit();before=history(c)
    engine=create_engine('sqlite:///'+str(path));db=SimpleNamespace(SessionLocal=sessionmaker(bind=engine))
    try:
        if entry=='drain_pending':assert asyncio.run(notices.drain_pending(db,lambda *a:pytest.fail('NETWORK')))==0
        else:
            with pytest.raises(hold.CancellationReplayHeld):asyncio.run(getattr(notices,entry)(db,'cancel:synthetic-2',1,'cancelled','SYNTHETIC',lambda *a:pytest.fail('NETWORK')))
    finally:engine.dispose()
    assert history(c)==before


def test_legal_same_order_new_notice_and_new_cancel_not_starved(pair):
    c,payload,_=pair;c.execute("UPDATE telegram_notification_events SET status='pending',attempts=0 WHERE event_key='cancel:synthetic-2'")
    c.execute("INSERT INTO orders VALUES(3,1,'TEST3','-123',83,'cancelled')");c.execute("INSERT INTO order_cancellation_facts VALUES('normal',3,'2026-11-03T10:00:00+00:00','SYNTHETIC')")
    body=dict(text='❌ 订单已取消\n\n订单号：TEST3\n取消原因：SYNTHETIC',reply_to_message_id=83)
    c.execute("INSERT INTO telegram_notification_events VALUES('cancel:normal',1,'-123','cancelled',?,'pending',0,'2026-11-03T10:00:00+00:00',NULL,NULL)",(json.dumps(body),));c.commit();calls=[]
    assert worker.deliver_one(c,'2026-11-01T00:00:00+00:00',lambda p:(calls.append(p) or (True,'ok')))=='sent'
    assert len(calls)==1 and calls[0]['reply_to_message_id']==83
    assert tuple(c.execute("SELECT status,attempts FROM telegram_notification_events WHERE event_key='cancel:synthetic-2'").fetchone())==('pending',0)
    payload=dict(payload,text='同一订单新的合法账务通知')
    assert send_message('SYNTHETIC',payload,event_key='new-account-notice',state=SimpleNamespace(reserve=lambda *a:0),transport=lambda *a,**k:SimpleNamespace(status_code=200,json=lambda:{'ok':True,'result':{'message_id':123}}))==(True,'ok')


def test_other_ambiguous_event_never_admitted(pair):
    import importlib.util
    path=Path(__file__).resolve().parents[1]/'tools/verify_cancel_15920_gate.py'
    spec=importlib.util.spec_from_file_location('joint_notice_gate',path);tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
    c,_,_=pair;assert tool.check(c,'2026-11-01T00:00:00+00:00')['remaining_unreviewed']==0
    c.execute("INSERT INTO telegram_notification_events SELECT 'unreviewed',user_id,chat_id,'payment',payload,'sending',1,created_at,NULL,NULL FROM telegram_notification_events WHERE event_key='cancel:synthetic-2'")
    with pytest.raises(RuntimeError,match='UNREVIEWED_AMBIGUOUS'):tool.check(c,'2026-11-01T00:00:00+00:00')


def test_post_send_database_busy_preserves_sending_and_does_not_resend(pair):
    # Reproduce the existing durable-claim / final-metadata failure window.
    # This is synthetic evidence of a possible path, not proof of historical ACK.
    c,_,_=pair
    c.execute("INSERT INTO orders VALUES(3,1,'TEST3','-123',83,'cancelled')")
    c.execute("INSERT INTO order_cancellation_facts VALUES('normal',3,'2026-11-03T10:00:00+00:00','SYNTHETIC')")
    payload=dict(text='❌ 订单已取消\n\n订单号：TEST3\n取消原因：SYNTHETIC',reply_to_message_id=83)
    c.execute("INSERT INTO telegram_notification_events VALUES('cancel:normal',1,'-123','cancelled',?,'pending',0,'2026-11-03T10:00:00+00:00',NULL,NULL)",(json.dumps(payload),));c.commit();calls=[]
    class InjectedConnection:
        def __getattr__(self,key):return getattr(c,key)
        def __setattr__(self,key,value):setattr(c,key,value)
        def execute(self,sql,args=()):
            if 'SET status=?,last_error=?,sent_at=?' in sql:raise sqlite3.OperationalError('database is locked')
            return c.execute(sql,args)
    assert worker.tick(InjectedConnection(),'2026-11-01T00:00:00+00:00',lambda p:(calls.append(p) or (True,'ok')))=='deferred_database_busy'
    assert tuple(c.execute("SELECT status,attempts,sent_at FROM telegram_notification_events WHERE event_key='cancel:normal'").fetchone())==('sending',1,None)
    before=history(c)
    assert worker.deliver_one(c,'2026-11-01T00:00:00+00:00',lambda p:pytest.fail('REPLAY'))=='idle'
    assert len(calls)==1 and history(c)==before
