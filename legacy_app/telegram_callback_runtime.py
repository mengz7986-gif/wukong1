"""Durable callback dispatch using the existing encrypted, leased job queue."""
import re
import asyncio
import logging
import time
import hashlib
logger=logging.getLogger(__name__)
ROUTES=(
 (r'^order_(confirm|cancel)(:[A-Za-z0-9_-]+)?$','handle_order_callback'),
 (r'^ai_flavor:','handle_ai_flavor_callback'),
 (r'^draft_cancel(?::|$)','handle_draft_cancel'),
 (r'^name_generate:','handle_name_generate'),
 (r'^address_confirm:','handle_name_generate'),
 (r'^account:','handle_account_callback'),
 (r'^manual_review:','handle_manual_review_callback'),
 (r'^(menu:|cat:|prod:|qty:|quick:|cart:|noop$)','handle_menu_callback'),
)
def target(data):
 return next((method for pattern,method in ROUTES if re.match(pattern,str(data or ''))),None)

def trace(query):
 # Only a one-way correlation tag and registered handler; never data/nonces,
 # customer text, API credentials or raw exception messages.
 tag=hashlib.sha256(str(getattr(query,'id','unknown')).encode()).hexdigest()[:16]
 return 'trace=%s handler=%s' % (tag,target(getattr(query,'data',None)) or 'unknown')

def error_code(error):
 kind=type(error).__name__
 if kind=='BadRequest':
  message=str(error).lower()
  if 'query is too old' in message or 'query id is invalid' in message:return 'QUERY_EXPIRED_OR_INVALID'
  return 'BAD_REQUEST_UNCLASSIFIED'
 return {'RetryAfter':'RATE_LIMIT','TimedOut':'NETWORK_TIMEOUT',
         'TimeoutError':'ACK_DEADLINE','NetworkError':'NETWORK_ERROR',
         'Forbidden':'FORBIDDEN'}.get(kind,'OTHER')

async def acknowledge(query):
 started=time.monotonic()
 try:
  # Feedback must reach Telegram before long business processing can expire
  # the query. Business completion remains in the original edited message.
  await asyncio.wait_for(query.answer('已收到，正在处理，请勿重复点击。'),timeout=2)
  logger.info('CALLBACK_PHASE phase=ack %s elapsed_ms=%d',trace(query),int((time.monotonic()-started)*1000))
 except Exception as error:
  # An expired answer must not discard a received business callback.
  logger.warning('CALLBACK_PHASE phase=ack_error %s code=%s elapsed_ms=%d',trace(query),error_code(error),int((time.monotonic()-started)*1000))

class AcknowledgedQuery:
 def __init__(self,raw):self.raw=raw
 def __getattr__(self,key):return getattr(self.raw,key)
 async def answer(self,text=None,**kwargs):
  # Repeated/expired-button feedback belongs to the clicker's Telegram toast,
  # not a new group message. Expired feedback cannot undo business processing.
  if text:
   try:
    return await self.raw.answer(text,**kwargs)
   except Exception as error:
    logger.warning('CALLBACK_PHASE phase=feedback_error %s code=%s',trace(self.raw),error_code(error))

class CallbackUpdate:
 def __init__(self,raw):
  self.raw=raw;self.callback_query=AcknowledgedQuery(raw.callback_query)
 def __getattr__(self,key):return getattr(self.raw,key)

async def dispatch(owner,update,context):
 method=target(update.callback_query.data)
 if not method:
  await AcknowledgedQuery(update.callback_query).answer('按钮已失效，请使用当前菜单或重新发送订单。')
  return
 started=time.monotonic()
 logger.info('CALLBACK_PHASE phase=process %s',trace(update.callback_query))
 from ai_work_budget import work_budget
 from request_memo import request_memo_scope
 from rule_dictionary import runtime_snapshot
 with runtime_snapshot(),request_memo_scope(),work_budget():
  await getattr(owner,method)(CallbackUpdate(update),context)
 logger.info('CALLBACK_PHASE phase=processed %s elapsed_ms=%d',trace(update.callback_query),int((time.monotonic()-started)*1000))
