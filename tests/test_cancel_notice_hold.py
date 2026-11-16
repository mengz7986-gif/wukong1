"""Exact cancellation hold: synthetic identity/payload, no Telegram network."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'legacy_app'))
import cancel_notice_hold as hold
import cancel_notice_worker as worker
from telegram_delivery_sync import send_message


@pytest.fixture
def event(monkeypatch, tmp_path):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: (_ for _ in ()).throw(AssertionError('REAL_NETWORK_FORBIDDEN')))
    # Original15920 unit checks stay independent; joint mandatory verification
    # is exercised by test_cancel18086_hold against both real companion calls.
    monkeypatch.setattr(hold,'verify_18086',lambda c:dict(isolated_unit_companion=True))
    c = sqlite3.connect(tmp_path / 'events.db')
    c.row_factory = sqlite3.Row
    c.executescript('''CREATE TABLE telegram_notification_events(event_key TEXT PRIMARY KEY,user_id INTEGER,chat_id TEXT,kind TEXT,payload TEXT,status TEXT,attempts INTEGER,created_at TEXT,sent_at TEXT,last_error TEXT);
    CREATE TABLE order_cancellation_facts(event_id TEXT,order_id INTEGER,cancelled_at TEXT,reason TEXT);
    CREATE TABLE orders(id INTEGER,user_id INTEGER,order_number TEXT,source_chat_id TEXT,source_message_id INTEGER,status TEXT);
    CREATE TABLE users(id INTEGER,telegram_id TEXT);''')
    c.execute("INSERT INTO users VALUES(1,'123')")
    c.execute("INSERT INTO orders VALUES(1,1,'TEST1','-123',88,'cancelled')")
    c.execute("INSERT INTO order_cancellation_facts VALUES('synthetic',1,'2026-11-02T09:16:37+00:00','测试')")
    payload = dict(chat_id='-123', text='❌ 订单已取消\n\n订单号：TEST1\n取消原因：测试', reply_to_message_id=88)
    c.execute('INSERT INTO telegram_notification_events VALUES(?,?,?,?,?,?,?,?,NULL,NULL)',
              ('cancel:synthetic',1,'-123','cancelled',json.dumps({k:v for k,v in payload.items() if k!='chat_id'}),'sending',1,'2026-11-02T09:16:37+00:00'))
    c.commit()
    monkeypatch.setattr(hold,'ORDER_ID',1)
    monkeypatch.setattr(hold,'EVENT_REF',hold.event_ref('cancel:synthetic'))
    monkeypatch.setattr(hold,'ROW_SHA256',hold.digest(dict(c.execute('SELECT * FROM telegram_notification_events').fetchone())))
    monkeypatch.setattr(hold,'FACT_SHA256',hold.digest(dict(c.execute('SELECT * FROM order_cancellation_facts').fetchone())))
    monkeypatch.setattr(hold,'ARTIFACT_SHA256',hold.artifact(payload))
    # Cooperative drain fixture is isolated; no live maintenance state touched.
    monkeypatch.setenv('RAINBOW_DEPLOY_DRAIN_PATH',str(tmp_path/'drain.json'))
    yield c,payload,tmp_path
    c.close()


def before(c):
    return [tuple(r) for r in c.execute('SELECT * FROM telegram_notification_events ORDER BY event_key')]


def test_verified_history_unchanged(event):
    c,_,_=event; old=before(c);changes=c.total_changes
    assert hold.verify(c)['historical_state']=='sending'
    assert before(c)==old and c.total_changes==changes


@pytest.mark.parametrize('sql',[
    "UPDATE telegram_notification_events SET status='pending'",
    'UPDATE telegram_notification_events SET attempts=2',
    "UPDATE telegram_notification_events SET sent_at='claimed-success'",
    'DELETE FROM telegram_notification_events',
    "UPDATE order_cancellation_facts SET reason='changed'",
    "INSERT INTO order_cancellation_facts VALUES('other',1,'2026-11-02T10:00:00+00:00','other')",
    "UPDATE orders SET status='pending'",
])
def test_gate_drift_fails_closed(event,sql):
    c,_,_=event;c.execute(sql)
    with pytest.raises(RuntimeError,match='CANCEL_15920_'):hold.verify(c)


def test_duplicate_artifact_gate_rejected(event):
    c,_,_=event;c.execute("INSERT INTO telegram_notification_events SELECT 'cancel:clone',user_id,chat_id,kind,payload,'pending',0,created_at,NULL,NULL FROM telegram_notification_events")
    with pytest.raises(RuntimeError,match='DUPLICATE_ARTIFACT'):hold.verify(c)


@pytest.mark.parametrize('status,attempts',[('sending',1),('pending',1),('pending',0),('unknown',1)])
def test_worker_never_claims_original_even_rearmed(event,status,attempts,caplog):
    c,_,_=event;c.execute('UPDATE telegram_notification_events SET status=?,attempts=?',(status,attempts));c.commit();old=before(c)
    assert worker.deliver_one(c,'2026-11-01T00:00:00+00:00',lambda p:pytest.fail('REPLAY'))=='idle'
    assert before(c)==old
    if status=='pending' and attempts==0:assert 'attempt_created=false' in caplog.text


def test_normal_cancellation_not_starved_by_held_pending(event):
    c,_,_=event;c.execute("UPDATE telegram_notification_events SET status='pending',attempts=0")
    c.execute("INSERT INTO orders VALUES(2,1,'TEST2','-123',89,'cancelled')")
    c.execute("INSERT INTO order_cancellation_facts VALUES('normal',2,'2026-11-02T10:00:00+00:00','测试')")
    payload={'text':'❌ 订单已取消\n\n订单号：TEST2\n取消原因：测试','reply_to_message_id':89}
    c.execute("INSERT INTO telegram_notification_events VALUES('cancel:normal',1,'-123','cancelled',?,'pending',0,'2026-11-02T10:00:00+00:00',NULL,NULL)",(json.dumps(payload),));c.commit();calls=[]
    assert worker.deliver_one(c,'2026-11-01T00:00:00+00:00',lambda p:(calls.append(p) or (True,'ok')))=='sent'
    assert len(calls)==1 and calls[0]['reply_to_message_id']==89
    assert tuple(c.execute("SELECT status,attempts FROM telegram_notification_events WHERE event_key='cancel:synthetic'").fetchone())==('pending',0)
    assert worker.deliver_one(c,'2026-11-01T00:00:00+00:00',lambda p:pytest.fail('DUPLICATE'))=='idle'


@pytest.mark.parametrize('mode',['key','raw','no_reply','modified_key_payload','notice_context','cancel_context'])
def test_direct_transport_blocked_before_pacing_or_network(event,mode,caplog):
    _,payload,_=event;payload=dict(payload);kwargs={}
    from notice_observation import begin,end
    token=None
    if mode in ('key','modified_key_payload'):kwargs['event_key']='cancel:synthetic'
    if mode=='modified_key_payload':payload['text']='changed'
    if mode=='no_reply':payload.pop('reply_to_message_id')
    if mode=='notice_context':
        token=begin('cancel:synthetic');payload['text']='changed'
    if mode=='cancel_context':
        token=hold.CURRENT_EVENT.set(hold.EVENT_REF);payload['text']='changed'
    try:
        with pytest.raises(hold.CancellationReplayHeld):
            send_message('synthetic-token',payload,transport=lambda *a,**k:pytest.fail('NETWORK'),state=SimpleNamespace(reserve=lambda *a:pytest.fail('PACING')),**kwargs)
    finally:
        if mode=='notice_context':end(token)
        if mode=='cancel_context':hold.CURRENT_EVENT.reset(token)
    assert 'attempt_created=false' in caplog.text


def test_parallel_direct_denials_and_context_cleanup(event):
    _,payload,_=event
    def run(_):
        try:send_message('synthetic',payload,transport=lambda *a,**k:pytest.fail('REPLAY'))
        except hold.CancellationReplayHeld:return True
    with ThreadPoolExecutor(max_workers=4) as ex:assert all(ex.map(run,range(48)))
    with pytest.raises(hold.CancellationReplayHeld):
        with hold.sending('cancel:synthetic'):pytest.fail('ENTERED')
    assert hold.CURRENT_EVENT.get() is None
    with pytest.raises(ValueError):
        with hold.sending('cancel:new'):raise ValueError('injected')
    assert hold.CURRENT_EVENT.get() is None


def test_legitimate_same_order_new_notification_allowed(event):
    _,payload,_=event;payload=dict(payload,text='同一订单的新账务通知');calls=[]
    result=send_message('synthetic',payload,event_key='new-account-notice',state=SimpleNamespace(reserve=lambda *a:0),transport=lambda *a,**k:(calls.append(1) or SimpleNamespace(status_code=200,json=lambda:{'ok':True,'result':{'message_id':123}})))
    assert result==(True,'ok') and calls==[1]


@pytest.mark.parametrize('entry',['dispatch','_dispatch_active','drain_pending'])
def test_account_paths_rearmed_event_blocked_without_mutation(event,entry,monkeypatch):
    from sqlalchemy import create_engine,text
    from sqlalchemy.orm import sessionmaker
    import account_notifications as notices
    c,_,path=event;c.execute("UPDATE telegram_notification_events SET status='pending',attempts=0");c.commit();old=before(c)
    engine=create_engine('sqlite:///'+str(path/'events.db'));db=SimpleNamespace(SessionLocal=sessionmaker(bind=engine))
    monkeypatch.setattr(notices.accounts,'payment_notice_key',lambda s,key,*a:key)
    monkeypatch.setattr(notices.accounts,'queue_notice',lambda *a:pytest.fail('QUEUE_WRITE'))
    async def send(*a,**k):pytest.fail('REPLAY')
    try:
        if entry=='drain_pending':assert asyncio.run(notices.drain_pending(db,send))==0
        else:
            with pytest.raises(hold.CancellationReplayHeld):
                asyncio.run(getattr(notices,entry)(db,'cancel:synthetic',1,'cancelled','synthetic',send))
        assert before(c)==old
    finally:engine.dispose()


def test_failed_normal_send_does_not_rearm_held_history(event):
    c,_,_=event;old=before(c)
    def failed(*a):raise TimeoutError('injected')
    # Current original is not even handed to the failing network callback.
    assert worker.tick(c,'2026-11-01T00:00:00+00:00',failed)=='idle'
    assert before(c)==old


def test_pending_recovery_keeps_normal_event_after_held(event,monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import account_notifications as notices
    c,_,path=event;c.execute("UPDATE telegram_notification_events SET status='pending',attempts=0")
    c.execute("INSERT INTO telegram_notification_events SELECT 'new-notice',user_id,chat_id,'payment',payload,'pending',0,'2026-11-02T10:00:00+00:00',NULL,NULL FROM telegram_notification_events");c.commit()
    engine=create_engine('sqlite:///'+str(path/'events.db'));db=SimpleNamespace(SessionLocal=sessionmaker(bind=engine));calls=[]
    async def dispatch(*args):calls.append(args[1])
    monkeypatch.setattr(notices,'dispatch',dispatch)
    try:assert asyncio.run(notices.drain_pending(db,lambda:None,limit=1))==1
    finally:engine.dispose()
    assert calls==['new-notice']


def test_concurrent_rearmed_pending_is_never_claimed(event):
    c,_,path=event;c.execute("UPDATE telegram_notification_events SET status='pending',attempts=0");c.commit();old=before(c)
    def claim(_):
        with sqlite3.connect(path/'events.db',timeout=3) as connection:
            return worker.claim(connection,'2026-11-01T00:00:00+00:00')
    with ThreadPoolExecutor(max_workers=4) as ex:assert list(ex.map(claim,range(32)))==[None]*32
    assert before(c)==old


def test_async_thread_context_preserves_event_identity(event):
    from notice_observation import begin,end
    async def send():
        token=begin('cancel:synthetic')
        try:
            await asyncio.to_thread(send_message,'synthetic',dict(chat_id='-999',text='changed'),transport=lambda *a,**k:pytest.fail('NETWORK'))
        finally:end(token)
    with pytest.raises(hold.CancellationReplayHeld):asyncio.run(send())
    from notice_observation import CURRENT
    assert CURRENT.get() is None


def test_transport_failure_after_normal_send_does_not_poison_identity(event):
    import requests
    _,payload,_=event;payload=dict(payload,text='normal cancellation');calls=[]
    def failed(*a,**k):calls.append(1);raise requests.exceptions.Timeout()
    with hold.sending('cancel:normal'):
        assert send_message('synthetic',payload,transport=failed,state=SimpleNamespace(reserve=lambda *a:0))==(False,'telegram_timeout_delivery_unknown')
    assert calls==[1] and hold.CURRENT_EVENT.get() is None
    with pytest.raises(hold.CancellationReplayHeld):hold.guard_event('cancel:synthetic','after_failure')


def test_companion_cannot_admit_other_sending(event):
    import importlib.util
    spec=importlib.util.spec_from_file_location('cancel_companion',Path(__file__).resolve().parents[1]/'tools/verify_cancel_15920_gate.py')
    tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
    c,_,_=event;old=before(c)
    assert tool.check(c,'2026-11-01T00:00:00+00:00')['remaining_unreviewed']==0
    assert before(c)==old
    c.execute("INSERT INTO telegram_notification_events SELECT 'new-sending',user_id,chat_id,'payment',payload,'sending',1,created_at,NULL,NULL FROM telegram_notification_events")
    with pytest.raises(RuntimeError,match='UNREVIEWED_AMBIGUOUS'):tool.check(c,'2026-11-01T00:00:00+00:00')
