"""Queue probe concurrency; synthetic messages, no Telegram or live DB."""
import sys,json,time,threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'legacy_app'),str(Path(__file__).parent)]
from test_four_specials_pipeline import pipeline


def test_identical_cold_classification_is_singleflight_without_pool(pipeline,monkeypatch):
    import contact_fields
    p=pipeline;q=p.queue;calls=[];original=contact_fields.parse_contact
    def counted(body):
        calls.append(1);assert p.db.engine.pool.checkedout()==0
        time.sleep(.04);return original(body)
    monkeypatch.setattr(contact_fields,'parse_contact',counted)
    raw=json.dumps(p.payload()).encode()
    row=dict(message_id=1,fingerprint='synthetic',payload=q.cipher.encrypt(raw).decode())
    gate=threading.Barrier(8)
    def run(_):gate.wait();return q._independent_complete(row)
    with ThreadPoolExecutor(max_workers=8) as pool:assert all(pool.map(run,range(8)))
    assert len(calls)==1
    assert not q._classification_locks  # bounded in-flight locks, not permanent keys


def test_different_payloads_do_not_share_global_parse_lock(pipeline,monkeypatch):
    import contact_fields
    q=pipeline.queue;original=contact_fields.parse_contact;gate=threading.Barrier(2)
    def parse(body):gate.wait(timeout=5);return original(body)
    monkeypatch.setattr(contact_fields,'parse_contact',parse)
    def run(i):
        raw=json.dumps(pipeline.payload(i)).encode()
        return q._independent_complete(dict(message_id=i,fingerprint=str(i),payload=q.cipher.encrypt(raw).decode()))
    with ThreadPoolExecutor(max_workers=2) as pool:assert all(pool.map(run,[1,2]))


def test_probe_releases_connection_before_order_classification(pipeline,monkeypatch):
    p=pipeline;p.receive(1);p.receive(2)
    first=p.queue.claim(now=time.time()+1)
    observed=[];original=p.queue._classify_independent
    def classify(row):
        observed.append(p.db.engine.pool.checkedout());return original(row)
    monkeypatch.setattr(p.queue,'_classify_independent',classify)
    job=p.queue.claim(now=time.time()+1)
    try:assert job and observed==[0]
    finally:p.release(job);p.release(first)


def test_index_hint_preserves_original_candidates_and_order(pipeline):
    from sqlalchemy import event
    p=pipeline
    for i in range(1,10):p.receive(i)
    with p.db.engine.begin() as c:
        c.exec_driver_sql("UPDATE telegram_order_jobs SET state='COMPLETED' WHERE message_id=3")
        c.exec_driver_sql("UPDATE telegram_order_jobs SET state='PROCESSING',lease_until=? WHERE message_id=4",(time.time()+100,))
        c.exec_driver_sql("UPDATE telegram_order_jobs SET state='FAILED_RETRYABLE',lease_until=? WHERE message_id=5",(time.time()-1,))
        c.exec_driver_sql("UPDATE telegram_order_jobs SET state='PROCESSING',lease_until=? WHERE message_id=6",(time.time()-1,))
        c.exec_driver_sql("UPDATE telegram_order_jobs SET state='REVIEW_REQUIRED' WHERE message_id=7")
    captured=[]
    def capture(c,cur,sql,args,ctx,many):
        if 'SELECT j.*, EXISTS' in sql:captured.append((sql,args))
    event.listen(p.db.engine,'before_cursor_execute',capture)
    try:
        for fenced in (True,False):
            p.queue.expiry_requires_fence=fenced
            for lane in (None,'message','callback'):
                with p.db.engine.connect() as c:
                    p.queue._candidate_rows(c,time.time()+1,lane)
                    sql,args=captured[-1]
                    old=sql.replace(' INDEXED BY ix_telegram_jobs_ready','').replace("j.state IN ('RECEIVED','FAILED_RETRYABLE','PROCESSING')\n                AND ",'',1)
                    assert list(c.exec_driver_sql(sql,args))==list(c.exec_driver_sql(old,args))
    finally:event.remove(p.db.engine,'before_cursor_execute',capture)
