"""Offline HTTP + real Bot handler harness; only Telegram transport is substituted."""
import os,json,time,asyncio,sys
from pathlib import Path
from types import SimpleNamespace
from four_specials_support import Pipeline

class FullChain:
 def __init__(self,path,representative=True):
  self.path=Path(path);self.path.mkdir(parents=True,exist_ok=True);os.chdir(self.path);(self.path/'static').mkdir(exist_ok=True)
  for key,name in {'AI_SETTINGS_PATH':'ai_settings.json','AI_DIAGNOSTICS_PATH':'diagnostics.json','RAINBOW_CONTACT_AUDIT_PATH':'contact-audit.jsonl','RAINBOW_LEARNING_EVIDENCE_PATH':'learning-evidence.json','RAINBOW_LEARNING_ACCESS_AUDIT_PATH':'learning-access.json','RAINBOW_LEARNING_USAGE_PATH':'learning-usage.json','RAINBOW_AI_USAGE_PATH':'ai-usage.json','RAINBOW_VERIFIED_ADDRESS_CACHE_PATH':'verified.sqlite3','RAINBOW_RECIPIENT_POLICY_PATH':'recipient-policy.json'}.items():os.environ[key]=str(self.path/name)
  self.pipeline=Pipeline(self.path/'runtime');p=self.pipeline
  p.queue.message_chat_limit=2;p.queue.expiry_requires_fence=True
  if representative:p.add_representative_reference()
  import warehouse_allocation,bundles
  with p.db.engine.begin() as c:warehouse_allocation.migrate(c);bundles.migrate(c)
  if representative:
   # Match the measured history sizes without retaining any production data.
   histories={'records':[dict(candidate_id=f'offline-history-{i}',commit_fingerprint=f'offline-{i}',expires_at='2099-01-01T00:00:00+00:00',padding='x'*3580) for i in range(2000)],'corrections':[]}
   Path(os.environ['RAINBOW_LEARNING_EVIDENCE_PATH']).write_text(json.dumps(histories))
   Path(os.environ['AI_DIAGNOSTICS_PATH']).write_text(json.dumps({'version':1,'calls':[dict(request_id=f'offline-{i}',at='2026-01-01T00:00:00+00:00',padding='x'*1210) for i in range(200)]}))
   with p.db.SessionLocal() as session:
    from models import Product
    category=session.get(Product,p.parent);product=session.get(Product,p.pid)
    cfg=dict(mode='fixed',units=1,components=[dict(product_id=p.pid,quantity=1)],prices={'regular':'10'},show_flavors=True)
    for i in range(2):bundles.save(session,category,f'合成福袋{i}',cfg,{p.pid:product},enabled=bool(i))
    session.commit()
  from models import DropshipAuthRequest
  with p.db.SessionLocal() as s:s.add(DropshipAuthRequest(user_id=p.user.id,telegram_id='123456789',status='approved'));s.commit()
  import models,api,bot,config,api_client
  from fastapi.testclient import TestClient
  models.db=p.db;api.db=p.db;api.SESSION_SECRET_KEY=p.secret;api.API_SECRET_KEY='offline-test-api'
  def sessions():
   with p.db.SessionLocal() as s:yield s
  api.app.dependency_overrides[api.get_db_session]=sessions
  self.http=TestClient(api.app,base_url='http://testserver')
  self.calls=[];self.sql=[]
  from sqlalchemy import event
  event.listen(p.db.engine,'before_cursor_execute',lambda c,cur,statement,parameters,context,many:self.sql.append(statement))
  class Adapter:
   def request(adapter,method,url,**kw):
    start=time.perf_counter();kw.pop('timeout',None)
    import contextvars
    response=contextvars.Context().run(self.http.request,method,url,**kw)
    if response.status_code>=400:print('OFFLINE_HTTP_FAILURE',url,response.text,flush=True)
    self.calls.append((method,url.split('testserver')[-1],response.status_code,(time.perf_counter()-start)*1000))
    return response
  client=api_client.APIClient();client.base_url='http://testserver';client.secret_key=api.API_SECRET_KEY;client._http_session=lambda:Adapter();bot.api_client=client
  self.bot=bot.OrderBot();self.sent=[];self.clocks={};self.traces={};self.trace_ids={};self.api_spans={};self.peak_leases=0
  import logging,re,ast,order_trace
  owner=self
  class Capture(logging.Handler):
   def emit(handler,record):
    line=record.getMessage()
    m=re.search(r'ORDER_SOURCE_CLOCK event=(\w+) source=([a-f0-9]+) clocks=(\{.*\})',line)
    if m:
     clock=ast.literal_eval(m[3]);holder=owner.clocks.setdefault(m[2],{})
     if m[1]!='QUEUE_DURABLE' or clock.get('inserted'):holder[m[1]]=clock
    m=re.search(r'ORDER_TRACE trace_id=([a-f0-9]+).*?source=([a-f0-9]+).*?stages=(\[.*\])',line)
    if m:owner.traces[m[2]]=ast.literal_eval(m[3]);owner.trace_ids[m[2]]=m[1]
    m=re.search(r'ORDER_API_SPANS trace_id=([a-f0-9]+).*?spans=(\[.*\]) spans_dropped=',line)
    if m:owner.api_spans[m[1]]=ast.literal_eval(m[2])
  self.capture=Capture();self.trace_level=order_trace.logger.level;order_trace.logger.setLevel(logging.INFO);order_trace.logger.addHandler(self.capture)
  self.pool_wait=[];acquire=p.db.engine.pool.connect
  def pool_connect():
   start=time.perf_counter()
   try:return acquire()
   finally:self.pool_wait.append((time.perf_counter()-start)*1000)
  p.db.engine.pool.connect=pool_connect
  os.environ['RAINBOW_PERFORMANCE_PATH']=str(self.path/'performance.json');os.environ['RAINBOW_RECEIPT_AUDIT_PATH']=str(self.path/'receipts.jsonl')
  owner=self
  class Telegram:
   id=123456789
   async def send_message(transport,chat_id,text,**kw):
    from telegram import Message
    owner.sent.append({'chat_id':chat_id,'text':text,'at':time.time()})
    return Message.de_json({'message_id':len(owner.sent)+1000,'date':int(time.time()),'chat':{'id':int(chat_id),'type':'group'},'text':text},transport)
  self.telegram=Telegram()
 def update(self,message=1):
  from telegram import Update
  payload=self.pipeline.payload(message,address=f'北京市朝阳区建国路{message}号');payload['message']['chat']['type']='group';payload['message']['from'].update(first_name='隔离用户',is_bot=False,username='synthetic_user')
  return Update.de_json(payload,self.telegram)
 def receive(self,message):
  update=self.update(message)
  return self.pipeline.queue.receive(update.effective_chat.id,message,update.effective_user.id,update.to_dict())
 async def handle(self,message=1,pre_received=False):
  from execution_safety import execution
  p=self.pipeline;update=self.update(message);payload=update.to_dict()
  if not pre_received:assert await asyncio.to_thread(p.queue.receive,update.effective_chat.id,message,update.effective_user.id,payload)
  durable=time.time();job=None
  while job is None:
   job=await asyncio.to_thread(p.queue.claim)
   if job is None:await asyncio.sleep(getattr(self,'claim_pause',.01))
  from sqlalchemy import text
  with p.db.engine.connect() as c:self.peak_leases=max(self.peak_leases,c.execute(text("SELECT count(*) FROM telegram_order_jobs WHERE state='PROCESSING'")).scalar())
  from telegram import Update
  update=Update.de_json(job['decoded_payload'],self.telegram)
  context=SimpleNamespace(user_data={},chat_data={},bot_data={},bot=self.telegram,application=SimpleNamespace(bot=self.telegram),_rainbow_queue_resume=True)
  try:
   with execution(job):await self.bot.handle_message(update,context)
   from telegram_queue_runtime import Runtime
   runtime=Runtime.__new__(Runtime);runtime.queue=p.queue
   final=await asyncio.to_thread(runtime.outcome,job);assert await asyncio.to_thread(p.queue.finish,job,final)
   import hashlib
   source=hashlib.sha256(('%s:%s:%s'%(job['namespace'],job['chat_id'],job['message_id'])).encode()).hexdigest()[:16]
   clocks=self.clocks[source];commit=clocks.get('TRANSACTION_COMMITTED')
   stages=self.traces.get(source,[])
   spans=self.api_spans.get(self.trace_ids.get(source),[])
   write_start=next((s[2] for s in spans if s[0]=='ORDER_WRITE_LOCK_WAIT'),None);write_end=next((s[2] for s in spans if s[0]=='ORDER_COMMIT'),None)
   return {'state':final,'durable_at':durable,'finished_at':time.time(),'persist_to_commit_ms':(commit['committed_at']-clocks['QUEUE_DURABLE']['durable_at'])*1000 if commit else None,'parser_ms':[ms for name,ms in stages if name=='PARSER_EXTRACT'],'queue_ms':(clocks['WORKER_CLAIMED']['claimed_at']-clocks['QUEUE_DURABLE']['durable_at'])*1000,'write_lock_hold_ms':write_end-write_start if write_start is not None and write_end is not None else None,'stages':stages}
  finally:p.release(job)
 def close(self):
  import order_trace
  order_trace.logger.removeHandler(self.capture);order_trace.logger.setLevel(self.trace_level);self.http.close();self.pipeline.close()
