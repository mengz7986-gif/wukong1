"""Web/API notifications use the same project-wide pacing as the Bot SDK."""
import logging
import threading
import time
import requests
from telegram_delivery_state import DeliveryState

logger=logging.getLogger(__name__)
_state=DeliveryState()
_pending=threading.BoundedSemaphore(256)


def send_message(token,payload,*,transport=None,state=None,sleep=time.sleep,retries=2,retry_timeout=False,event_key=None):
    from cancel_notice_hold import guard_transport
    guard_transport(payload, event_key)
    transport=transport or requests.post
    state=state or _state
    if not _pending.acquire(blocking=False):
        return False,'telegram_delivery_queue_full'
    try:
        chat=str(payload.get('chat_id',''))
        for attempt in range(retries+1):
            while True:
                delay=state.reserve(chat,0.04,3.1 if chat.startswith('-') or chat.startswith('@') else 1.05)
                if delay<=0:break
                sleep(delay)
            try:
                response=transport('https://api.telegram.org/bot'+token+'/sendMessage',json=payload,timeout=15)
                data=response.json()
                from notice_observation import observe
                observe('TRANSPORT_RESULT',detail='ok' if response.status_code==200 and data.get('ok') else 'telegram_http_'+str(response.status_code),
                    attempt=attempt+1,http_status=response.status_code,
                    message_id=(data.get('result') or {}).get('message_id') if isinstance(data.get('result'),dict) else None)
                if response.status_code==200 and data.get('ok'):
                    return True,'ok'
                retry_after=data.get('parameters',{}).get('retry_after')
                if response.status_code==429 and isinstance(retry_after,(float,int)) and retry_after>=0:
                    state.cooldown(float(retry_after)+0.1)
                    if attempt<retries:continue
                    return False,'telegram_retry_after_exhausted'
                description=str(data.get('description') or '').lower()
                if response.status_code==400 and payload.get('reply_to_message_id') and any(
                    reason in description for reason in ('message to be replied not found','reply message not found','replied message not found','message_id_invalid')):
                    return False,'telegram_reply_unavailable'
                return False,'telegram_http_'+str(response.status_code)
            except requests.exceptions.Timeout as exc:
                from notice_observation import observe
                observe('TRANSPORT_EXCEPTION',error=exc,attempt=attempt+1)
                logger.warning('telegram_delivery endpoint=sendMessage kind=timeout attempt=%d',attempt+1)
                if not retry_timeout:
                    # Telegram may have accepted a request before the connection
                    # timed out. Do not blindly duplicate receipts or bills.
                    return False,'telegram_timeout_delivery_unknown'
                if attempt==retries:return False,'telegram_timeout_exhausted'
                sleep(min(2**attempt,4))
            except (requests.exceptions.RequestException,ValueError) as exc:
                from notice_observation import observe
                observe('TRANSPORT_EXCEPTION',error=exc,attempt=attempt+1)
                return False,'telegram_transport_or_response_error'
    finally:
        _pending.release()
