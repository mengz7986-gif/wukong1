"""Exact six historical receipt safety; synthetic DB and transport only."""
import sys,os,json,time,hashlib,asyncio,socket,copy
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'legacy_app'),str(Path(__file__).parent)]
from test_four_specials_pipeline import pipeline
import six_unknown_receipt_hold as hold

@pytest.fixture
def held(pipeline,monkeypatch,tmp_path):
 from sqlalchemy import text
 from success_receipt_audit import append
 from frozen_direct_receipts import terminal_links
 p=pipeline;path=tmp_path/'audit.jsonl';monkeypatch.setenv('RAINBOW_RECEIPT_AUDIT_PATH',str(path))
 monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:(_ for _ in ()).throw(AssertionError('REAL_NETWORK_FORBIDDEN')))
 cases=[];pins=[]
 for i in range(1,7):
  addr=f'北京市朝阳区建国路{800+i}号';assert p.receive(i,address=addr)
  job=p.queue.claim(now=time.time()+1);assert job and job['message_id']==i
  try:order=p.create(i,job=job,address=addr);assert p.queue.finish(job,'COMPLETED')
  finally:p.release(job)
  full=hashlib.sha256(f"{job['namespace']}:{job['chat_id']}:{job['message_id']}".encode()).hexdigest();row=dict(attempt_id=f'synthetic-{i}',task_id=full[:16],order_numbers=[order.order_number],delivery_path='DIRECT_BOT',outbox_event_key=None,method='send_message')
  append(dict(row,status='SENDING'));append(dict(row,status='UNKNOWN',message_id=None,result_kind='AMBIGUOUS'))
  chain=[json.loads(x) for x in path.read_text().splitlines()][-2:]
  with p.db.engine.connect() as c:
   jobrow=dict(c.execute(text('SELECT * FROM telegram_order_jobs WHERE message_id=:i'),{'i':i}).mappings().one())
  import sqlite3
  with sqlite3.connect(p.path/'orders.sqlite3') as c:
   pin=dict(order_id=order.id,attempt_ref=hashlib.sha256(row['attempt_id'].encode()).hexdigest(),source_identity_sha256=full,source_pair_sha256=hashlib.sha256(f"{job['chat_id']}:{i}".encode()).hexdigest(),number_sha256=hashlib.sha256(order.order_number.encode()).hexdigest(),original_sending_sha256=hold.digest(chain[0]),unknown_row_sha256=hold.digest(chain[1]),terminal_links_sha256=hold.digest(terminal_links(c,chain[1])),job_row_sha256=hold.digest(jobrow))
  cases.append((job,order.id,order.order_number));pins.append(pin)
 monkeypatch.setattr(hold,'APPROVED_ORDER_IDS',tuple(p['order_id'] for p in pins));monkeypatch.setattr(hold,'SCOPE',tuple(pins))
 return p,cases,path

class Bot:
 def __init__(self):self.calls=[]
 async def send_message(self,**kw):
  self.calls.append(kw);await asyncio.sleep(0);return SimpleNamespace(message_id=1000+len(self.calls),chat=SimpleNamespace(id=int(kw['chat_id']),type='supergroup'))
 async def edit_message_text(self,**kw):return await self.send_message(**kw)

def latest(path):return [json.loads(x) for x in path.read_text().splitlines() if json.loads(x)['status']=='UNKNOWN']

@pytest.mark.parametrize('case',range(6))
def test_direct_original_rejected_before_intent_with_or_without_execution(held,case,caplog):
 from success_receipt_audit import ReceiptSender
 from execution_safety import execution
 p,cases,path=held;job,oid,number=cases[case];before=path.read_bytes();bot=Bot()
 async def run():
  for flags in ({},{'_automatic_receipt':True}):
   with pytest.raises(hold.ReceiptReplayHeld):await ReceiptSender(bot,dict(order_number=number,**flags)).send_message(chat_id=job['chat_id'],text='SYNTHETIC')
  with execution(job):
   with pytest.raises(hold.ReceiptReplayHeld):await ReceiptSender(bot,dict(order_number='different')).edit_message_text(chat_id=job['chat_id'],text='SYNTHETIC')
 asyncio.run(run());assert not bot.calls and path.read_bytes()==before
 assert 'attempt_created=false' in caplog.text


def test_injected_pending_six_held_without_starving_new_receipt(held):
 from sqlalchemy import text
 from execution_safety import deliver,_queue_receipt
 p,cases,path=held;before=path.read_bytes()
 with p.db.engine.begin() as c:
  for job,oid,_ in cases:
   key='final:'+hashlib.sha256(f"{job['namespace']}:{job['chat_id']}:{job['message_id']}".encode()).hexdigest()+':0'
   c.execute(text("INSERT INTO telegram_execution_receipts(event_key,chat_id,source_message_id,body,status,created_at) VALUES(:key,:chat,:message,'SYNTHETIC','pending',1)"),dict(key=key,chat=job['chat_id'],message=job['message_id']))
  c.execute(text("INSERT INTO telegram_execution_receipts(event_key,chat_id,source_message_id,body,status,created_at) VALUES('final:new:0','-999',999,'NEW','pending',2)"))
 bot=Bot();asyncio.run(deliver(p.db,bot,limit=0));assert not bot.calls
 async def concurrent():await asyncio.gather(*(deliver(p.db,bot,limit=1) for _ in range(3)))
 asyncio.run(concurrent());assert len(bot.calls)==1 and bot.calls[0]['text']=='NEW'
 with p.db.engine.connect() as c:
  rows=c.execute(text("SELECT status,attempts FROM telegram_execution_receipts WHERE event_key!='final:new:0'" )).all();assert rows==[('pending',0)]*6
 with p.db.SessionLocal() as s:
  for job,oid,_ in cases:assert _queue_receipt(s,job,(job['namespace'],job['chat_id'],job['message_id']),[oid],'TEST') is False
 assert path.read_bytes()==before


def test_transport_guard_and_legitimate_notice_same_order(held):
 from telegram_delivery import TelegramDeliveryLimiter
 from account_notifications import dispatch
 p,cases,path=held;job,oid,number=cases[0];before=path.read_bytes();calls=[]
 limiter=TelegramDeliveryLimiter(group_interval=0,global_interval=0)
 async def network(*args,**kw):calls.append(1);return True
 async def run():
  data=dict(chat_id=job['chat_id'],reply_to_message_id=job['message_id'],text='SYNTHETIC')
  with pytest.raises(hold.ReceiptReplayHeld):await limiter.process_request(network,(),{},'sendMessage',data,{'order_receipt':True})
  async def send(chat,message,**kw):
   await limiter.process_request(network,(),{},'sendMessage',dict(chat_id=chat,text=message,**kw),None);return True,'ok'
  result=await dispatch(p.db,'notice:new:order:'+str(oid),p.user.id,'manual_bill','NEW LEGITIMATE NOTICE',send)
  assert result['sent']
 asyncio.run(run());assert len(calls)==1 and path.read_bytes()==before


def test_normal_new_order_and_completed_recovery_unchanged(held):
 from execution_safety import recover_dispatches,resolve
 p,cases,path=held;before=p.quantities();assert p.queue.claim(now=time.time()+1) is None
 for job,_,_ in cases:assert resolve(p.db,job) is None
 assert recover_dispatches(p.db)['automatically_rearmed']==0
 assert p.receive(7,address='北京市朝阳区建国路999号');job=p.queue.claim(now=time.time()+1)
 try:assert p.create(7,job=job,address='北京市朝阳区建国路999号');assert p.queue.finish(job,'COMPLETED')
 finally:p.release(job)
 assert p.quantities()==(before[0]-1,before[1]-1)
 from success_receipt_audit import ReceiptSender
 bot=Bot();asyncio.run(ReceiptSender(bot,dict(order_number='NEW',_automatic_receipt=True)).send_message(chat_id=-999,text='NEW'))
 assert len(bot.calls)==1


@pytest.mark.parametrize('fault',['row','missing','sending','job','history','pending','extra_source','pin'])
def test_companion_fails_closed_for_drift(held,fault,monkeypatch):
 from sqlalchemy import text
 import sqlite3
 p,cases,path=held;rows=latest(path)
 if fault=='pin':
  pins=copy.deepcopy(hold.SCOPE);pins[0]['number_sha256']='f'*64;monkeypatch.setattr(hold,'SCOPE',pins)
 if fault=='row':rows[0]['method']='changed'
 if fault=='missing':rows=rows[:-1]
 if fault=='sending':rows[0]['status']='SENDING'
 if fault=='job':
  with p.db.engine.begin() as c:c.execute(text("UPDATE telegram_order_jobs SET attempts=2 WHERE message_id=1"))
 if fault=='pending':
  with p.db.engine.begin() as c:c.execute(text("INSERT INTO telegram_execution_receipts(event_key,chat_id,source_message_id,body,status,created_at) VALUES('injected','-1000000000001',1,'TEST','pending',1)"))
 if fault=='history':path.write_text(path.read_text().replace('"status": "SENDING"','"status": "NOT_SENT"',1))
 if fault=='extra_source':
  from success_receipt_audit import append
  append(dict(rows[0],attempt_id='extra',status='SENDING'));append(dict(rows[0],attempt_id='extra',status='UNKNOWN'))
 with sqlite3.connect(p.path/'orders.sqlite3') as c:
  with pytest.raises(ValueError):hold.partition_verified(rows,c)


def test_companion_readonly_and_concurrent_direct_denials(held):
 import sqlite3
 from success_receipt_audit import ReceiptSender
 p,cases,path=held;before=path.read_bytes();counts=p.counts();stock=p.quantities()
 with sqlite3.connect(p.path/'orders.sqlite3') as c:
  c.execute('PRAGMA query_only=ON');remaining,accepted=hold.partition_verified(latest(path),c);assert not remaining and len(accepted)==6
 def reject(i):
  job,oid,number=cases[i%6]
  with pytest.raises(hold.ReceiptReplayHeld):asyncio.run(ReceiptSender(Bot(),dict(order_number=number)).send_message(chat_id=job['chat_id'],text='TEST'))
 with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(reject,range(48)))
 assert path.read_bytes()==before and p.counts()==counts and p.quantities()==stock


def test_forced_requeued_source_recovers_without_new_business_or_receipt(held):
 from sqlalchemy import text
 from execution_safety import resolve
 from telegram_queue_runtime import Runtime
 p,cases,path=held;before=path.read_bytes();counts=p.counts();stock=p.quantities()
 with p.db.engine.begin() as c:c.execute(text("UPDATE telegram_order_jobs SET state='RECEIVED' WHERE message_id=1"))
 job=p.queue.claim(now=time.time()+1);assert job and job['message_id']==1
 runtime=Runtime.__new__(Runtime);runtime.queue=p.queue
 try:
  assert runtime.recoverable_outcome(job)=='COMPLETED'
  result=resolve(p.db,job,'SYNTHETIC_RECOVERY');assert not result['receipt_queued']
 finally:p.release(job)
 assert path.read_bytes()==before and p.counts()==counts and p.quantities()==stock


def test_guard_fault_occurs_before_audit_or_network(held,monkeypatch):
 from success_receipt_audit import ReceiptSender
 p,cases,path=held;before=path.read_bytes();bot=Bot()
 def fault(*a):raise RuntimeError('SYNTHETIC_GUARD_UNAVAILABLE')
 monkeypatch.setattr(hold,'scope',fault)
 with pytest.raises(RuntimeError):asyncio.run(ReceiptSender(bot,dict(order_number=cases[0][2])).send_message(chat_id=-1,text='TEST'))
 assert not bot.calls and path.read_bytes()==before


def test_original_thirteen_review_rules_still_require_exact_ack():
 import sqlite3
 import receipt_review_gate as old
 c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
 c.executescript('CREATE TABLE orders(id,order_number,source_chat_id,source_message_id);CREATE TABLE telegram_execution_commits(namespace,chat_id,message_id,order_id);CREATE TABLE telegram_order_jobs(namespace,chat_id,message_id,state,lease_token,lease_until);CREATE TABLE telegram_execution_receipts(event_key,status,attempts,delivered_message_id,error_code,chat_id,source_message_id);')
 rows=[];records=[]
 for i,(aid,number) in enumerate(old.REVIEW_V5_EXACT_SCOPE.items(),1):
  identity=hashlib.sha256(f'shared:-999:{i}'.encode()).hexdigest();key='final:'+identity+':0'
  c.execute('INSERT INTO orders VALUES(?,?,?,?)',(i,number,'-999',i));c.execute('INSERT INTO telegram_execution_commits VALUES(?,?,?,?)',('shared','-999',i,i));c.execute("INSERT INTO telegram_order_jobs VALUES(?,?,?,'COMPLETED',NULL,NULL)",('shared','-999',i));c.execute("INSERT INTO telegram_execution_receipts VALUES(?,'sent',1,?,NULL,?,?)",(key,1000+i,'-999',i))
  row=dict(attempt_id=aid,status='UNKNOWN',order_numbers=[number],task_id=identity[:16]);rows.append(row)
  ack=dict(c.execute('SELECT * FROM telegram_execution_receipts WHERE event_key=?',(key,)).fetchone())
  records.append(dict(attempt_id=aid,order_number=number,original_send_status='UNKNOWN',final_ack_is_not_original_send_success=True,no_resend=True,user_authorization='SYNTHETIC',original_attempt_sha256=old.digest(row),final_event_key=key,final_delivered_message_id=1000+i,final_receipt_sha256=old.digest(ack)))
 review=dict(version=5,approval_state='APPROVED',records=records);before=c.total_changes
 assert len(old.validate(review,rows,c))==13 and c.total_changes==before
 c.execute("UPDATE telegram_execution_receipts SET delivered_message_id=NULL WHERE source_message_id=1")
 with pytest.raises(ValueError):old.validate(review,rows,c)
 c.close()


def test_missing_production_history_does_not_silently_pass(tmp_path,monkeypatch):
 from success_receipt_audit import assert_quiet
 monkeypatch.setenv('RAINBOW_RECEIPT_AUDIT_PATH',str(tmp_path/'absent.jsonl'))
 monkeypatch.setenv('DATABASE_URL','sqlite:////var/lib/newbot/live/synthetic.db')
 with pytest.raises(RuntimeError,match='SIX_HOLD_HISTORY_REQUIRED'):assert_quiet()


def test_held_recovery_alias_and_multipart_identity(held):
 _,cases,_=held
 for pin,(job,_,_) in zip(hold.SCOPE,cases):
  for part in range(3):assert hold.held_recovery('final:'+pin['source_identity_sha256']+':'+str(part),'-other',999)
  assert hold.held_recovery('arbitrary-alias',job['chat_id'],job['message_id'])
  assert not hold.held_recovery('new-event','-new',999)


def test_legacy_agent_success_entry_cannot_bypass_hold(held):
 from bot import OrderBot
 p,cases,path=held;before=path.read_bytes();calls=[]
 owner=OrderBot.__new__(OrderBot)
 async def api(*a,**kw):return True,{'order_number':cases[0][2]}
 async def reply(*a,**kw):calls.append(1)
 owner._api_call=api;owner._reply_agent_group_message=reply
 update=SimpleNamespace(message=object());context=SimpleNamespace(user_data={})
 with pytest.raises(hold.ReceiptReplayHeld):asyncio.run(owner._record_agent_group_order(update,context,{}))
 assert not calls and path.read_bytes()==before


def test_id_only_order_success_calls_remain_held(held):
 from success_receipt_audit import ReceiptSender
 _,cases,path=held;before=path.read_bytes();bot=Bot()
 async def run():
  for job,oid,_ in cases:
   with pytest.raises(hold.ReceiptReplayHeld):await ReceiptSender(bot,dict(id=oid)).send_message(chat_id=job['chat_id'],text='TEST')
 asyncio.run(run());assert not bot.calls and path.read_bytes()==before


def test_production_queue_start_awaits_exact_gate_before_workers(held,monkeypatch):
 from bot import OrderBot
 import success_receipt_audit
 _,_,_=held;calls=[]
 monkeypatch.setenv('NEWBOT_INGESTION_POLICY_PATH','/etc/newbot/ingestion-policy.json')
 def reject():calls.append('review');raise RuntimeError('SYNTHETIC_GATE_STOP')
 monkeypatch.setattr(success_receipt_audit,'assert_quiet',reject)
 owner=OrderBot.__new__(OrderBot);owner._queue_runtime=None
 with pytest.raises(RuntimeError,match='SYNTHETIC_GATE_STOP'):asyncio.run(owner._start_durable_queue(None))
 assert calls==['review'] and owner._queue_runtime is None


def test_string_identity_and_missing_identity_cannot_bypass(held):
 from success_receipt_audit import ReceiptSender
 _,cases,path=held;before=path.read_bytes();bot=Bot()
 async def run():
  for job,oid,number in cases:
   for facts in [dict(order_numbers=number),dict(id=' '+str(oid)+' '),{}]:
    with pytest.raises(hold.ReceiptReplayHeld):await ReceiptSender(bot,facts).send_message(chat_id=job['chat_id'],text='TEST')
 asyncio.run(run());assert not bot.calls and path.read_bytes()==before


def test_dispatch_transport_does_not_retain_selection_read_lock(tmp_path):
 import sqlite3
 from sqlalchemy import create_engine
 from sqlalchemy.orm import sessionmaker
 from execution_safety import deliver
 path=tmp_path/'delete-journal.sqlite3';engine=create_engine('sqlite:///'+str(path));database=SimpleNamespace(SessionLocal=sessionmaker(bind=engine))
 with sqlite3.connect(path) as c:
  c.executescript("CREATE TABLE telegram_execution_receipts(event_key TEXT PRIMARY KEY,chat_id TEXT,source_message_id INTEGER,body TEXT,edit_message_id INTEGER,status TEXT,attempts INTEGER DEFAULT 0,created_at REAL,delivered_message_id INTEGER,error_code TEXT);CREATE TABLE unrelated_business(id INTEGER);")
  c.executemany("INSERT INTO telegram_execution_receipts VALUES(?, '-new', ?, 'NEW', NULL,'pending',0,?,NULL,NULL)",[(f'final:new:{i}',i,float(i)) for i in range(2)])
 class Transport:
  async def send_message(self,**kw):
   # A different connection must be able to commit while network work runs.
   with sqlite3.connect(path,timeout=.1) as c:c.execute('INSERT INTO unrelated_business VALUES(1)')
   return SimpleNamespace(message_id=1)
 asyncio.run(deliver(database,Transport(),limit=1))
 with sqlite3.connect(path) as c:
  assert c.execute('SELECT count(*) FROM unrelated_business').fetchone()[0]==1
  assert c.execute("SELECT count(*) FROM telegram_execution_receipts WHERE status='sent'").fetchone()[0]==1
 engine.dispose()
