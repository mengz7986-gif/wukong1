"""Controlled notice diagnostics only; cannot change delivery or retry policy."""
import contextvars,hashlib,json,logging,re,time
CURRENT=contextvars.ContextVar('notice_delivery_observation',default=None)
logger=logging.getLogger(__name__)
def begin(key):
 return CURRENT.set(dict(event_ref=hashlib.sha256(str(key).encode()).hexdigest(),started=time.monotonic()))
def end(token):CURRENT.reset(token)
def observe(stage,*,detail=None,error=None,attempt=0,http_status=None,message_id=None):
 try:
  scope=CURRENT.get() or {}
  code='SUCCESS' if detail=='ok' else 'UNCLASSIFIED';phase='response';chain=[]
  if detail and (re.fullmatch(r'telegram_http_[1-5][0-9]{2}',str(detail)) or detail in {
    'telegram_timeout_delivery_unknown','telegram_retry_after_exhausted','telegram_reply_unavailable',
    'telegram_transport_or_response_error','telegram_delivery_queue_full','delivery_unknown'}):code=detail
  if error is not None:
   if isinstance(error,ValueError):code='INVALID_RESPONSE';phase='decode';chain=[{'type':'ValueError'}]
   else:
    from network_safety import classify
    classified=classify(error);code=classified['code'];phase=classified['phase'];chain=classified['exception_chain']
  if stage not in {'DISPATCH_RESULT','DISPATCH_EXCEPTION','TRANSPORT_RESULT','TRANSPORT_EXCEPTION'}:return
  status=http_status if type(http_status) is int and 100<=http_status<=599 else 0
  mid=message_id if type(message_id) is int and message_id>0 else 0
  logger.info('NOTICE_DELIVERY_DIAGNOSTIC event_ref=%s stage=%s code=%s phase=%s exception_chain=%s attempt=%d http_status=%d message_id=%d elapsed_ms=%.3f',
   scope.get('event_ref','unscoped'),stage,code,phase,json.dumps(chain,separators=(',',':')),attempt if type(attempt) is int else 0,status,mid,
   max(0,(time.monotonic()-scope.get('started',time.monotonic()))*1000))
 except Exception:
  # Observability must not prevent a receipt or change its durable state.
  pass
