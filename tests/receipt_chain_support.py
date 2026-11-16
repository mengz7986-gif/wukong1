"""Existing full chain with its real limiter and explicitly simulated Telegram HTTP."""
import asyncio,time,json,hashlib
from types import SimpleNamespace
from full_chain_support import FullChain

class ReceiptChain(FullChain):
 def __init__(self,path,latency=.20,representative=True):
  super().__init__(path,representative)
  from telegram_delivery import TelegramDeliveryLimiter
  from telegram_delivery_state import DeliveryState
  self.claim_pause=.1;self.latency=latency;self.updates={};self.acks=[];self.http_attempts=[];self.errors=[];self._mid_sources={}
  self.limiter=TelegramDeliveryLimiter(state=DeliveryState(self.path/'delivery-state.json'))
  owner=self
  class Telegram:
   id=123456789
   rate_limiter=owner.limiter
   async def call(transport,endpoint,data,policy=None):
    from execution_safety import CURRENT
    from order_trace import CURRENT as TRACE
    job=(CURRENT.get() or {}).get('job') or {};trace=TRACE.get() or {}
    source=trace.get('receipt_message_id') or owner._mid_sources.get(data.get('message_id')) or job.get('message_id')
    async def network(method,payload):
     owner.http_attempts.append({'endpoint':method,'at':time.time(),'source':source})
     delay=(owner.latency[len(owner.http_attempts)%len(owner.latency)] if isinstance(owner.latency,list) else owner.latency)
     await asyncio.sleep(delay)
     if owner.errors:raise owner.errors.pop(0)
     if method=='answerCallbackQuery':return True
     from telegram import Message
     mid=payload.get('message_id') or len(owner.acks)+1000
     now=time.time();owner._mid_sources[mid]=source
     row={'source':source,'chat_id':int(payload['chat_id']),'at':now,'message_id':mid,'endpoint':method,
          'success_receipt':bool((policy or {}).get('order_receipt')) or str(payload.get('text','')).startswith('✅ 录单成功')}
     owner.acks.append(row);owner.sent.append(dict(row,text=payload.get('text','')))
     return Message.de_json({'message_id':mid,'date':int(now),'chat':{'id':int(payload['chat_id']),'type':'group'},'text':payload.get('text','')},transport)
    return await owner.limiter.process_request(network,(endpoint,data),{},endpoint,data,policy)
   async def send_message(transport,chat_id,text,rate_limit_args=None,**kw):
    return await transport.call('sendMessage',dict(kw,chat_id=chat_id,text=text),rate_limit_args)
   async def edit_message_text(transport,text,chat_id=None,message_id=None,rate_limit_args=None,**kw):
    return await transport.call('editMessageText',dict(kw,chat_id=chat_id,message_id=message_id,text=text),rate_limit_args)
   async def answer_callback_query(transport,callback_query_id,**kw):
    return await transport.call('answerCallbackQuery',dict(callback_query_id=callback_query_id,**kw))
  self.telegram=Telegram();self.bot._queue_runtime=SimpleNamespace(queue=self.pipeline.queue)
 def prepare(self,message,*,sender=123456789,chat=-1000000000001,body=None,reply=None):
  from telegram import Update
  payload=self.pipeline.payload(message,address=f'北京市朝阳区建国路{message}号')
  payload['message']['from'].update(id=sender,first_name='隔离用户',is_bot=False,username='offline')
  payload['message']['chat'].update(id=chat,type='group')
  if body is not None:payload['message']['text']=body
  if reply is not None:payload['message']['reply_to_message']=self.updates[reply].to_dict()['message']
  self.updates[message]=Update.de_json(payload,self.telegram)
  return self.updates[message]
 def update(self,message=1):
  return self.updates.get(message) or self.prepare(message)
 def customer(self,telegram_id):
  from models import DropshipAuthRequest
  user=self.pipeline.db.create_user(str(telegram_id),username='offline')
  with self.pipeline.db.SessionLocal() as s:s.add(DropshipAuthRequest(user_id=user.id,telegram_id=str(telegram_id),status='approved'));s.commit()
  return user
 def receipt_result(self,message):
  update=self.updates[message];acks=[r for r in self.acks if r['source']==message and r['success_receipt']]
  original=update.message.date.timestamp()
  return {'message':message,'source_date':original,'ack_at':max((r['at'] for r in acks),default=None),
          'source_to_ack_ms':(max(r['at'] for r in acks)-original)*1000 if acks else None,'ack_parts':len(acks)}
 async def callback(self,update_id,data,message,*,sender=123456789):
  from telegram import Update
  from execution_safety import execution
  from telegram_queue_runtime import Runtime
  payload={'update_id':update_id,'callback_query':{'id':str(update_id),'from':{'id':sender,'is_bot':False,'first_name':'隔离用户'},'chat_instance':'offline','data':data,'message':message.to_dict()}}
  update=Update.de_json(payload,self.telegram);key=-update_id;p=self.pipeline
  assert await asyncio.to_thread(p.queue.receive,update.effective_chat.id,key,sender,payload)
  job=await asyncio.to_thread(p.queue.claim,lane='callback');assert job
  runtime=Runtime.__new__(Runtime);runtime.queue=p.queue;runtime.owner=self.bot;runtime.executions={}
  context=SimpleNamespace(user_data={},chat_data={},bot_data={},bot=self.telegram,_rainbow_queue_resume=True)
  try:
   await runtime.guarded_handler(job,update,context,True)
   assert await asyncio.to_thread(p.queue.finish,job,'COMPLETED')
  finally:p.release(job)
