import os,sys,asyncio,time,json,threading
from pathlib import Path
import pytest
sys.path[:0]=[str(Path(os.getenv('RECEIPT_TEST_SOURCE',str(Path(__file__).resolve().parents[1])))/'legacy_app'),str(Path(__file__).parent)]

class Clock:
 def __init__(self):self.now=1000.;self.sent=[]
 def __call__(self):return self.now
 async def sleep(self,delay):self.now+=delay;await asyncio.sleep(0)

def test_receipt_burst_respects_one_second_and_20_per_rolling_minute():
 from telegram_delivery import TelegramDeliveryLimiter
 c=Clock();limiter=TelegramDeliveryLimiter(clock=c,sleep=c.sleep)
 async def network():c.sent.append(c.now);return True
 async def exercise():
  for _ in range(25):await limiter.process_request(network,(),{},'sendMessage',{'chat_id':-123},{'order_receipt':True})
 asyncio.run(exercise())
 assert all(b-a>=1.049 for a,b in zip(c.sent,c.sent[1:]))
 assert all(sum(t-60<x<=t for x in c.sent)<=20 for t in c.sent)
 assert c.sent[3]-c.sent[0]<3.2 and c.sent[20]-c.sent[0]>=60

def test_shared_state_enforces_quota_across_instances_and_preserves_legacy(tmp_path,monkeypatch):
 import telegram_delivery_state as module
 c=Clock();monkeypatch.setattr(module.time,'time',lambda:c.now)
 path=tmp_path/'state.json';a=module.DeliveryState(path);b=module.DeliveryState(path)
 for i in range(20):
  assert (a if i%2 else b).reserve('-123',.04,1.05)==0;c.now+=1.05
 assert a.reserve('-123',.04,1.05)>38
 path.write_text(json.dumps({'global_next':c.now,'cooldown':0,'chats':{'-123':c.now+3.1}}))
 assert a.reserve('-123',.04,1.05)>=3.09
 assert len(json.loads(path.read_text())['group_sends']['-123'])==20

def test_timeout_not_retried_but_retry_after_is_rejected_then_retried():
 from telegram_delivery import TelegramDeliveryLimiter
 from telegram.error import TimedOut,RetryAfter
 c=Clock();limiter=TelegramDeliveryLimiter(clock=c,sleep=c.sleep);calls=[]
 async def timeout():calls.append(c.now);raise TimedOut()
 async def limited():
  calls.append(c.now)
  if len(calls)==1:raise RetryAfter(2)
  return True
 async def exercise():
  with pytest.raises(TimedOut):await limiter.process_request(timeout,(),{},'sendMessage',{'chat_id':-1},{'order_receipt':True})
  assert len(calls)==1;calls.clear()
  assert await limiter.process_request(limited,(),{},'sendMessage',{'chat_id':-2},{'order_receipt':True})
 asyncio.run(exercise());assert len(calls)==2 and calls[1]-calls[0]>=2

@pytest.fixture
def chain(tmp_path,monkeypatch):
 monkeypatch.setenv('NEWBOT_ROLE','split-release-only');monkeypatch.setenv('NEWBOT_SPLIT_MAPPINGS_ONLY','1');monkeypatch.setenv('RAINBOW_DISABLE_BACKGROUND_WORKERS','1');monkeypatch.delenv('NEWBOT_INGESTION_POLICY_PATH',raising=False);monkeypatch.chdir(tmp_path)
 import models
 monkeypatch.setattr(models,'db',None)
 from receipt_chain_support import ReceiptChain
 before=dict(os.environ);c=ReceiptChain(tmp_path,latency=.01)
 yield c
 c.close()
 for k in set(os.environ)-set(before):os.environ.pop(k,None)
 for k,v in before.items():os.environ[k]=v

def test_learning_cannot_delay_success_ack(chain,monkeypatch):
 import learning_pipeline
 observed=[]
 def learning(preview,order):
  observed.append(bool(chain.acks));time.sleep(.1)
 monkeypatch.setattr(learning_pipeline,'record_committed',learning)
 r=asyncio.run(chain.handle(1));assert r['state']=='COMPLETED' and observed==[True]
 audit=[json.loads(l) for l in (chain.path/'receipts.jsonl').read_text().splitlines()]
 assert audit[-1]['status']=='SENT' and audit[-1]['ack_at']<=audit[-1]['at']
 assert chain.receipt_result(1)['source_to_ack_ms']>0

def test_direct_timeout_cannot_create_recovery_duplicate(chain):
 from telegram.error import TimedOut
 from sqlalchemy import text
 from execution_safety import resolve,deliver
 chain.errors=[TimedOut()]
 with pytest.raises(TimedOut):asyncio.run(chain.handle(1))
 p=chain.pipeline;before=p.counts();stock=p.quantities()
 with p.db.engine.connect() as c:job=dict(c.execute(text('SELECT * FROM telegram_order_jobs WHERE message_id=1')).mappings().one())
 result=resolve(p.db,job,'ORDER_EXCEPTION');assert result['state']=='COMPLETED' and not result['receipt_queued']
 asyncio.run(deliver(p.db,chain.telegram));assert len(chain.http_attempts)==1
 assert p.counts()==before and p.quantities()==stock and before['orders']==1
 assert chain.receipt_result(1)['ack_at'] is None

def test_complete_ack_requires_last_chunk_and_precedes_slow_audit(monkeypatch):
 from order_trace import CURRENT,receipt_plan,receipt_ack
 record={'started':time.perf_counter(),'stages':[],'receipt_source_at':100.,'receipt_source':'offline'}
 token=CURRENT.set(record)
 try:
  receipt_plan(2);receipt_ack(1,101.)
  assert 'receipt_complete_ack_at' not in record
  receipt_ack(2,103.);assert record['receipt_complete_ack_at']==103.
 finally:CURRENT.reset(token)


def test_rollback_retains_used_quota_for_old_limiter(tmp_path,monkeypatch):
 import telegram_delivery_state as module
 c=Clock();monkeypatch.setattr(module.time,'time',lambda:c.now)
 path=tmp_path/'state.json';state=module.DeliveryState(path)
 for _ in range(4):
  assert state.reserve('-123',.04,1.05)==0;c.now+=1.05
 before=json.loads(path.read_text())['group_sends']
 until=state.fence_legacy_rollback();after=json.loads(path.read_text())
 assert after['group_sends']==before and until>=max(before['-123'])+60
 assert after['cooldown']>c.now and state.reserve('-123',.04,3.1)>58

def test_destination_mismatch_never_counts_complete_ack():
 from order_trace import CURRENT,receipt_plan,receipt_ack
 record={'started':time.perf_counter(),'stages':[],'receipt_source_at':100.,'receipt_chat_sha256':'expected'}
 token=CURRENT.set(record)
 try:
  receipt_plan(1);receipt_ack(1,101.,'different')
  assert 'receipt_complete_ack_at' not in record
 finally:CURRENT.reset(token)

def test_success_ack_followed_by_recovery_cannot_resend(chain):
 from execution_safety import resolve,deliver
 from sqlalchemy import text
 asyncio.run(chain.handle(1));p=chain.pipeline;before=p.counts();stock=p.quantities()
 with p.db.engine.begin() as c:
  c.execute(text("UPDATE telegram_order_jobs SET state='PROCESSING',lease_token='post-ack-test',lease_until=:until WHERE message_id=1"),{'until':time.time()+120})
  job=dict(c.execute(text('SELECT * FROM telegram_order_jobs WHERE message_id=1')).mappings().one())
 assert not resolve(p.db,job,'AFTER_ACK_FAILURE')['receipt_queued']
 asyncio.run(deliver(p.db,chain.telegram))
 assert len(chain.http_attempts)==1 and p.counts()==before and p.quantities()==stock
 assert not chain.receive(1)

def test_rate_limited_receipt_retries_transport_only(chain):
 from telegram.error import RetryAfter
 chain.errors=[RetryAfter(.1)]
 r=asyncio.run(chain.handle(1));p=chain.pipeline
 assert r['state']=='COMPLETED' and p.counts()['orders']==1
 assert p.quantities()==(9999,9999) and len(chain.acks)==1 and len(chain.http_attempts)==2
 assert chain.receipt_result(1)['ack_at'] is not None

def test_cross_warehouse_confirmation_retains_original_clock_and_is_idempotent(chain):
 from models import Warehouse,Product
 from sqlalchemy import text
 p=chain.pipeline
 with p.db.SessionLocal() as session:
  w=Warehouse(name='隔离第二仓',address='测试',shipping_fee=0);session.add(w);session.flush()
  parent=Product(name='乙牌',regular_price=10,is_parent=True,default_warehouse_id=w.id,reference_price_state='SET');session.add(parent);session.flush()
  item=Product(name='苹果',parent_id=parent.id,regular_price=10,cost_price=2,default_warehouse_id=w.id,reference_price_state='SET');session.add(item);session.commit();wid=w.id;pid=item.id
 assert p.db.inbound_stock(pid,wid,100,unit_cost=2)['success']
 chain.prepare(1,body='甲牌 薄荷1\n乙牌 苹果1\n张三 13800000000\n北京市朝阳区建国路88号')
 first=asyncio.run(chain.handle(1));assert first['state']=='NEED_CONFIRMATION'
 with p.db.engine.connect() as c:token=c.execute(text('SELECT token FROM telegram_order_drafts WHERE message_id=1')).scalar_one()
 from telegram import Message
 prompt=chain.acks[-1]
 message=Message.de_json({'message_id':prompt['message_id'],'date':int(time.time()),'chat':{'id':-1000000000001,'type':'group'},'text':'待确认'},chain.telegram)
 asyncio.run(chain.callback(2001,'order_confirm:'+token,message))
 assert p.counts()['orders']==2
 assert chain.receipt_result(1)['ack_at'] is not None
 before=p.counts();attempts=len(chain.http_attempts)
 asyncio.run(chain.callback(2002,'order_confirm:'+token,message))
 assert p.counts()==before
 assert len([a for a in chain.acks if a['success_receipt']])==1


def test_full_multipart_ack_is_not_completed_after_partial_timeout(chain,monkeypatch):
 from telegram.error import TimedOut
 from order_trace import CURRENT
 from order_receipt import receipt_chunks
 from success_receipt_audit import ReceiptSender
 # Use the actual group renderer, failing only the second network part.
 calls=[];original=chain.telegram.send_message
 async def send(*args,**kwargs):
  calls.append(1)
  if len(calls)==2:raise TimedOut()
  return await original(*args,**kwargs)
 monkeypatch.setattr(chain.telegram,'send_message',send)
 facts=dict(_automatic_receipt=True,order_number='ISOLATED-MULTIPART',total_amount=600,total_shipping_fee=0,grand_total=600,user_debt=600,debt_before=0,debt_after=600,today_order_count=1,today_order_amount=600,items=[dict(product_name='薄荷'+str(i),quantity=1,parent_name='甲牌',warehouse_name='隔离仓') for i in range(600)])
 record={'started':time.perf_counter(),'stages':[],'receipt_source_at':time.time()}
 token=CURRENT.set(record)
 try:
  with pytest.raises(TimedOut):asyncio.run(chain.bot._send_order_confirmation(chain.telegram,-1000000000001,facts))
  assert record['receipt_parts']>1 and len(record['receipt_acks'])==1
  assert 'receipt_complete_ack_at' not in record
 finally:CURRENT.reset(token)

def test_audit_failure_after_ack_cannot_schedule_competing_receipt(chain,monkeypatch):
 import success_receipt_audit as audit
 from sqlalchemy import text
 from execution_safety import resolve
 original=audit.append
 def broken(row):
  if row['status']=='SENT':raise OSError('isolated audit fault')
  original(row)
 monkeypatch.setattr(audit,'append',broken)
 asyncio.run(chain.handle(1));p=chain.pipeline
 with p.db.engine.begin() as c:
  c.execute(text("UPDATE telegram_order_jobs SET state='PROCESSING',lease_token='after-ack',lease_until=:until WHERE message_id=1"),{'until':time.time()+120})
  job=dict(c.execute(text('SELECT * FROM telegram_order_jobs WHERE message_id=1')).mappings().one())
 assert not resolve(p.db,job)['receipt_queued'] and len(chain.acks)==1
 assert p.counts()['orders']==1 and p.quantities()==(9999,9999)
