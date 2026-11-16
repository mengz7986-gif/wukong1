import os,sys,json,time
from pathlib import Path
import pytest
from concurrent.futures import ThreadPoolExecutor

@pytest.fixture
def pipeline(tmp_path,monkeypatch):
 sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'legacy_app'));sys.path.insert(0,str(Path(__file__).parent))
 monkeypatch.setenv('NEWBOT_ROLE','split-release-only');monkeypatch.setenv('NEWBOT_SPLIT_MAPPINGS_ONLY','1');monkeypatch.delenv('NEWBOT_INGESTION_POLICY_PATH',raising=False)
 monkeypatch.chdir(tmp_path)
 from four_specials_support import Pipeline
 before=dict(os.environ);p=Pipeline(tmp_path/'runtime')
 yield p
 p.close()
 for k in set(os.environ)-set(before):os.environ.pop(k,None)
 for k,v in before.items():os.environ[k]=v

def test_durable_duplicate_message_and_transaction_idempotency(pipeline):
 p=pipeline
 assert p.receive(1) and not p.receive(1)
 job=p.queue.claim(now=time.time()+.2);assert job is not None
 try:
  order=p.create(1,job=job);assert order is not None
  before=p.counts();stock=p.quantities()
  with pytest.raises(ValueError,match='MESSAGE_ALREADY_PROCESSED'):p.create(1,job=job)
  assert p.counts()==before and p.quantities()==stock
  assert p.queue.finish(job,'COMPLETED')
 finally:p.release(job)
 assert p.quantities()==(9999,9999)

def test_different_messages_are_not_merged(pipeline):
 p=pipeline
 assert p.receive(1) and p.receive(2)
 for mid in (1,2):
  job=p.queue.claim(now=time.time()+.2);assert job is not None
  try:p.create(mid,job=job);assert p.queue.finish(job,'COMPLETED')
  finally:p.release(job)
 assert p.counts()['orders']==2 and p.quantities()==(9998,9998)

def test_late_expired_worker_cannot_commit(pipeline):
 p=pipeline;assert p.receive(1);job=p.queue.claim(now=time.time()+.2)
 from sqlalchemy import text
 with p.db.engine.begin() as c:c.execute(text("UPDATE telegram_order_jobs SET lease_until=:old WHERE message_id=1"),{'old':time.time()-1})
 try:
  with pytest.raises(ValueError,match='EXECUTION_CLOSED'):p.create(1,job=job)
  assert p.counts()['orders']==0 and p.quantities()==(10000,10000)
 finally:p.release(job)

def test_concurrent_same_message_has_one_financial_effect(pipeline):
 p=pipeline
 def create():
  try:return p.create(10).id
  except ValueError as e:
   assert 'MESSAGE_ALREADY_PROCESSED' in str(e);return None
 with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:create(),range(4)))
 assert sum(r is not None for r in results)==1
 assert p.counts()['orders']==1 and p.quantities()==(9999,9999)

def test_quantity_split_preserves_total_fifo_and_first_admission(pipeline):
 p=pipeline;order=p.create(1,qty=13)
 from models import Order,OrderItem
 from sqlalchemy import func
 with p.db.SessionLocal() as s:
  rows=s.query(Order).order_by(Order.id).all()
  assert len(rows)==3 and [o.status for o in rows]==['pending','split_pending','split_pending']
  assert s.query(func.sum(OrderItem.quantity)).scalar()==13
 assert p.quantities()==(9987,9987)

@pytest.fixture
def isolated_api(pipeline,tmp_path,monkeypatch):
 import models,importlib
 (tmp_path/'static').mkdir(exist_ok=True)
 monkeypatch.setattr(models,'db',pipeline.db)
 api=importlib.import_module('api');monkeypatch.setattr(api,'db',pipeline.db);monkeypatch.setattr(api,'SESSION_SECRET_KEY',pipeline.secret)
 return api

def test_manual_export_moves_lists_and_next_day_releases_once(pipeline,isolated_api,monkeypatch):
 from datetime import datetime,timedelta,timezone
 import telegram_source_identity,package_rules,split_management
 from models import Order
 p=pipeline;order=p.create(1,qty=13)
 base=package_rules.day_window_utc(datetime.utcnow())[0]
 monkeypatch.setattr(telegram_source_identity,'account_cutover',lambda:(base-timedelta(days=365)).replace(tzinfo=timezone.utc))
 def views():
  with p.db.SessionLocal() as s:
   return {v:{r['id'] for r in isolated_api.get_orders.__wrapped__(view=v,offset=0,session=s,_auth='test')} for v in ('pending','split','recorded')}
 before=views();assert [len(before[v]) for v in ('pending','split','recorded')]==[1,2,0]
 stock=p.quantities();counts=p.counts()
 with p.db.SessionLocal() as s:
  response=isolated_api.export_orders.__wrapped__(view='pending',session=s,_auth='test')
  assert response.status_code==200 and response.headers['x-order-export-count']=='1'
 after=views();assert [len(after[v]) for v in ('pending','split','recorded')]==[0,2,1]
 assert not split_management.tick(p.db,now=base+timedelta(hours=3))
 future=base+timedelta(days=1,hours=3)
 assert len(split_management.tick(p.db,now=future))==1
 assert not split_management.tick(p.db,now=future)
 final=views();assert [len(final[v]) for v in ('pending','split','recorded')]==[1,1,1]
 assert all(not final[a]&final[b] for a,b in [('pending','split'),('pending','recorded'),('split','recorded')])
 assert p.quantities()==stock and p.counts()==counts

def test_same_address_waits_for_predecessor_recording_and_beijing_day(pipeline,isolated_api,monkeypatch):
 from datetime import datetime,timedelta,timezone
 import telegram_source_identity,package_rules,split_management
 from models import Order
 p=pipeline;first=p.create(1);second=p.create(2)
 with p.db.SessionLocal() as s:assert [o.status for o in s.query(Order).order_by(Order.id)]==['pending','split_pending']
 base=package_rules.day_window_utc(datetime.utcnow())[0];next_day=base+timedelta(days=1,hours=1)
 monkeypatch.setattr(telegram_source_identity,'account_cutover',lambda:(base-timedelta(days=365)).replace(tzinfo=timezone.utc))
 assert not split_management.tick(p.db,now=next_day)
 with p.db.SessionLocal() as s:isolated_api.export_orders.__wrapped__(view='pending',session=s,_auth='test')
 assert not split_management.tick(p.db,now=base+timedelta(hours=3))
 assert split_management.tick(p.db,now=next_day)==[second.id]
 assert p.quantities()==(9998,9998)

def test_concurrent_cross_day_release_does_not_duplicate(pipeline,isolated_api,monkeypatch):
 from datetime import datetime,timedelta,timezone
 import telegram_source_identity,package_rules,split_management
 from sqlalchemy import text
 p=pipeline;p.create(1,qty=13)
 base=package_rules.day_window_utc(datetime.utcnow())[0];future=base+timedelta(days=1,hours=3)
 monkeypatch.setattr(telegram_source_identity,'account_cutover',lambda:(base-timedelta(days=365)).replace(tzinfo=timezone.utc))
 with p.db.SessionLocal() as s:isolated_api.export_orders.__wrapped__(view='pending',session=s,_auth='test')
 before=p.quantities()
 with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:split_management.tick(p.db,now=future),range(4)))
 assert sum(len(r) for r in results)==1
 with p.db.engine.connect() as c:
  assert c.execute(text('SELECT count(*) FROM (SELECT group_id,day,count(*) n FROM split_release_days GROUP BY group_id,day HAVING count(*)>1)')).scalar()==0
 assert p.quantities()==before

def test_pool_sessions_are_released_after_failed_transactions(pipeline):
 p=pipeline
 for _ in range(5):
  with pytest.raises(ValueError):p.db.create_order(p.user.id,[], '张三','13800000000','北京市朝阳区建国路88号')
 assert p.db.engine.pool.checkedout()==0

def test_restart_requeues_only_uncommitted_standalone_new_message(pipeline,tmp_path):
 import fcntl
 from sqlalchemy import text
 p=pipeline;assert p.receive(1);job=p.queue.claim(now=time.time()+.2);p.release(job)
 with (tmp_path/'bot.lock').open('a') as guard:
  fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
  result=p.queue.recover_after_restart(guard,after=time.time()-60)
 assert result['requeued']==1 and result['receipts_replayed']==0
 with p.db.engine.connect() as c:
  assert c.execute(text('SELECT state,lease_token FROM telegram_order_jobs')).one()==('RECEIVED',None)
  assert c.execute(text('SELECT count(*) FROM telegram_queue_recovery_audit')).scalar()==1
 new=p.queue.claim(now=time.time()+.2)
 try:
  assert new['lease_token']!=job['lease_token']
  with pytest.raises(ValueError,match='EXECUTION_CLOSED'):p.create(1,job=job)
  assert p.create(1,job=new) is not None
 finally:p.release(new)
 assert p.counts()['orders']==1 and p.quantities()==(9999,9999)

def test_restart_observes_commit_without_recreating_order_or_receipt(pipeline,tmp_path):
 import fcntl
 from sqlalchemy import text
 p=pipeline;assert p.receive(1);job=p.queue.claim(now=time.time()+.2)
 p.create(1,job=job);p.release(job);before=p.counts();stock=p.quantities()
 with (tmp_path/'bot.lock').open('a') as guard:
  fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
  result=p.queue.recover_after_restart(guard,after=time.time()-60)
 assert result['committed_observed']==1 and result['requeued']==0
 assert p.counts()==before and p.quantities()==stock
 with p.db.engine.connect() as c:
  assert c.execute(text('SELECT state FROM telegram_order_jobs')).scalar()=='COMPLETED'
  assert c.execute(text('SELECT count(*) FROM telegram_execution_receipts')).scalar()==0

def test_restart_does_not_rearm_unknown_or_sending(pipeline,tmp_path):
 import fcntl
 from sqlalchemy import text
 p=pipeline;assert p.receive(1);job=p.queue.claim(now=time.time()+.2);p.release(job)
 with p.db.engine.begin() as c:c.execute(text("INSERT INTO telegram_execution_receipts(event_key,chat_id,source_message_id,body,status,attempts,created_at) VALUES('unknown-test','-1000000000001',1,'test','unknown',1,:now)"),{'now':time.time()})
 with (tmp_path/'bot.lock').open('a') as guard:
  fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
  assert p.queue.recover_after_restart(guard,after=time.time()-60)['requeued']==0
 with p.db.engine.connect() as c:
  assert c.execute(text('SELECT status,attempts FROM telegram_execution_receipts')).one()==('unknown',1)
  assert c.execute(text('SELECT state FROM telegram_order_jobs')).scalar()=='PROCESSING'

def test_restart_never_touches_protected_incident_or_older_messages(pipeline,tmp_path):
 import fcntl
 from p0_recovery_protection import identity
 from sqlalchemy import text
 p=pipeline;assert p.receive(1);job=p.queue.claim(now=time.time()+.2);p.release(job)
 with p.db.engine.connect() as c:before=c.execute(text('SELECT * FROM telegram_order_jobs')).mappings().one()
 with (tmp_path/'bot.lock').open('a') as guard:
  fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
  assert p.queue.recover_after_restart(guard,after=time.time()-60,protected={identity(job)})['requeued']==0
  assert p.queue.recover_after_restart(guard,after=time.time()+60)['requeued']==0
 with p.db.engine.connect() as c:assert dict(c.execute(text('SELECT * FROM telegram_order_jobs')).mappings().one())==dict(before)

def test_process_exit_after_persistence_does_not_lose_message(pipeline,tmp_path):
 import subprocess
 from sqlalchemy import text
 p=pipeline
 code="""import os,sys,json
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telegram_order_queue import Queue
engine=create_engine(sys.argv[1]);q=Queue(SimpleNamespace(engine=engine,SessionLocal=sessionmaker(bind=engine)),sys.argv[2])
assert q.receive(-1000000000001,5,123456789,json.loads(sys.argv[3]))
os._exit(0)
"""
 env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'legacy_app'))
 subprocess.run([sys.executable,'-c',code,str(p.db.engine.url),p.secret,json.dumps(p.payload(5))],env=env,check=True)
 assert not p.receive(5)
 job=p.queue.claim(now=time.time()+.2)
 try:assert job is not None and p.create(5,job=job) is not None
 finally:p.release(job)
 assert p.counts()['orders']==1 and p.quantities()==(9999,9999)

def test_failed_receipt_never_recreates_order(pipeline):
 import asyncio
 from execution_safety import resolve,deliver
 from sqlalchemy import text
 p=pipeline;assert p.receive(1);job=p.queue.claim(now=time.time()+.2);p.create(1,job=job);p.release(job)
 with p.db.engine.begin() as c:c.execute(text('UPDATE telegram_order_jobs SET lease_until=:old'),{'old':time.time()-1})
 resolve(p.db,job,only_if_lease_expired=True)
 calls=[]
 class Sender:
  async def send_message(self,**kwargs):calls.append(1);raise asyncio.TimeoutError()
 before=p.counts();stock=p.quantities()
 asyncio.run(deliver(p.db,Sender()));asyncio.run(deliver(p.db,Sender()))
 assert len(calls)==1 and p.counts()==before and p.quantities()==stock
 with p.db.engine.connect() as c:assert c.execute(text('SELECT status,attempts FROM telegram_execution_receipts')).one()==('unknown',1)
