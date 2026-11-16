"""Additional coverage for the actual delay path; prior 41 tests remain evidence."""
import sys,os,asyncio,threading,time
from pathlib import Path
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'legacy_app'),str(Path(__file__).parent)]
from test_four_specials_pipeline import pipeline

@pytest.mark.parametrize('alias',['甲牌',' 甲 牌 ','ICE12K','İ牌','ı牌','K牌','K牌','ſ牌','s牌','正品甲牌'])
def test_heading_prefix_optimization_preserves_original_regex(alias):
 from category_evidence import _heading_match,_heading_pattern
 pattern=_heading_pattern(alias)
 for prefix in ['甲牌','甲 牌','ICE12K','ice12k','I牌','i牌','İ牌','ı牌','K牌','k牌','K牌','S牌','s牌','ſ牌','正品甲牌','其他牌','']:
  for suffix in ['',':薄荷2',' 薄荷2','一代','10000口','仓']:
   text=prefix+suffix;old=pattern.match(text) if pattern else None;new=_heading_match(alias,text)
   assert (old.span() if old else None)==(new.span() if new else None),(alias,text)

def test_source_guards_have_bounded_catalog_queries(pipeline):
 from sqlalchemy import event
 from rule_dictionary import runtime_snapshot
 from request_memo import request_memo_scope
 from address_source_guard import validate_persisted_region
 from source_coverage import validate_persisted_source_total
 p=pipeline;p.add_representative_reference();assert p.receive(1)
 queries=[]
 event.listen(p.db.engine,'before_cursor_execute',lambda c,cur,statement,parameters,context,many:queries.append(statement))
 with p.db.SessionLocal() as session,runtime_snapshot(),request_memo_scope():
  validate_persisted_region(session,-1000000000001,1,'北京市朝阳区建国路88号')
  validate_persisted_source_total(session,-1000000000001,1,[{'product_id':p.pid,'warehouse_id':p.wid,'quantity':1}])
 catalog_queries=[q for q in queries if q.lstrip().upper().startswith('SELECT') and 'FROM products' in q]
 assert len(catalog_queries)<=4,len(catalog_queries)
 with p.db.SessionLocal() as session:
  with pytest.raises(ValueError):validate_persisted_source_total(session,-1000000000001,1,[{'product_id':p.pid,'quantity':2}])
 assert p.quantities()==(10000,10000) and p.counts()['orders']==0

@pytest.fixture
def full_chain(tmp_path,monkeypatch):
 import models
 monkeypatch.setenv('NEWBOT_ROLE','split-release-only');monkeypatch.setenv('NEWBOT_SPLIT_MAPPINGS_ONLY','1');monkeypatch.delenv('NEWBOT_INGESTION_POLICY_PATH',raising=False);monkeypatch.setattr(models,'db',None)
 from full_chain_support import FullChain
 before=dict(os.environ);f=FullChain(tmp_path)
 yield f
 f.close()
 for k in set(os.environ)-set(before):os.environ.pop(k,None)
 for k,v in before.items():os.environ[k]=v

def test_real_http_handler_commit_receipt_and_duplicate_delivery(full_chain):
 f=full_chain;r=asyncio.run(f.handle(1));assert r['state']=='COMPLETED'
 before=f.pipeline.counts();stock=f.pipeline.quantities()
 assert not f.receive(1)
 assert f.pipeline.counts()==before and f.pipeline.quantities()==stock
 assert len(f.sent)==1 and f.sent[0]['chat_id']==-1000000000001
 assert '录单成功' in f.sent[0]['text'] and r['persist_to_commit_ms']>0
 assert f.pipeline.db.engine.pool.checkedout()==0

def test_full_handler_bounded_four_worker_burst(full_chain):
 f=full_chain
 for mid in range(1,9):assert f.receive(mid)
 async def process():
  gate=asyncio.Semaphore(4)
  async def worker():
   async with gate:return await f.handle(pre_received=True)
  return await asyncio.gather(*(worker() for _ in range(8)))
 rows=asyncio.run(process())
 assert all(r['state']=='COMPLETED' for r in rows)
 assert f.pipeline.counts()['orders']==8 and f.pipeline.quantities()==(9992,9992)
 assert len(f.sent)==8 and f.pipeline.db.engine.pool.checkedout()==0
 assert 1<=f.peak_leases<=2

def test_cancelled_local_work_keeps_capacity_until_thread_exits():
 from recognition_work import run
 release=threading.Event();started=threading.Event();active=0;maximum=0;lock=threading.Lock()
 def blocked():
  nonlocal active,maximum
  with lock:active+=1;maximum=max(maximum,active);started.set()
  release.wait(3)
  with lock:active-=1
 async def exercise():
  first=asyncio.create_task(run(blocked));second=asyncio.create_task(run(blocked))
  while active<2:await asyncio.sleep(.01)
  first.cancel();second.cancel();await asyncio.gather(first,second,return_exceptions=True)
  third=asyncio.create_task(run(blocked));await asyncio.sleep(.05)
  assert active==2 and not third.done()
  release.set();await asyncio.wait_for(third,3)
 asyncio.run(exercise());assert maximum==2

def test_source_edit_invalidates_prevalidation_memo(pipeline):
 import json
 from sqlalchemy import text
 from rule_dictionary import runtime_snapshot
 from request_memo import request_memo_scope
 from source_coverage import validate_persisted_source_total
 p=pipeline;assert p.receive(1)
 items=[{'product_id':p.pid,'warehouse_id':p.wid,'quantity':1}]
 with runtime_snapshot(),request_memo_scope():
  with p.db.SessionLocal() as session:validate_persisted_source_total(session,-1000000000001,1,items)
  changed=p.payload(1,qty=2);encrypted=p.queue.cipher.encrypt(json.dumps(changed).encode()).decode()
  with p.db.engine.begin() as c:c.execute(text('UPDATE telegram_order_jobs SET payload=:body WHERE message_id=1'),{'body':encrypted})
  with p.db.SessionLocal() as session:
   with pytest.raises(ValueError):validate_persisted_source_total(session,-1000000000001,1,items)
 assert p.quantities()==(10000,10000) and p.counts()['orders']==0

def test_cpu_prevalidation_does_not_hold_sqlite_writer(full_chain,monkeypatch):
 import address_source_guard
 from sqlalchemy import text
 original=address_source_guard.validate_persisted_region;count=0;writer_obtained=[]
 def checked(session,*a,**kw):
  nonlocal count
  count+=1
  if count==1:
   # A second connection must acquire a writer while prevalidation runs.
   with full_chain.pipeline.db.engine.connect() as other:
    other.exec_driver_sql('BEGIN IMMEDIATE');writer_obtained.append(True);other.rollback()
  return original(session,*a,**kw)
 monkeypatch.setattr(address_source_guard,'validate_persisted_region',checked)
 r=asyncio.run(full_chain.handle(1))
 assert r['state']=='COMPLETED' and writer_obtained==[True] and count==2

def test_optional_address_trust_revocation_is_not_hidden_by_quantity_memo(pipeline,monkeypatch):
 import cloud_address_trust
 from source_coverage import validate_persisted_source_total
 from rule_dictionary import runtime_snapshot
 from request_memo import request_memo_scope
 p=pipeline;assert p.receive(1)
 trusted=[True];monkeypatch.setattr(cloud_address_trust,'trusted',lambda address:trusted[0])
 items=[{'product_id':p.pid,'warehouse_id':p.wid,'quantity':1}]
 with runtime_snapshot(),request_memo_scope():
  with p.db.SessionLocal() as session:
   validate_persisted_source_total(session,-1000000000001,1,items,recipient_address='北京市海淀区建国路88号')
  trusted[0]=False
  with p.db.SessionLocal() as session:
   with pytest.raises(ValueError):validate_persisted_source_total(session,-1000000000001,1,items,recipient_address='北京市海淀区建国路88号')
