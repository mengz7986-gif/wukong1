"""Privacy-safe transport classification; retry only provable pre-send failures."""
import errno
import hashlib
import http.client
import json
import logging
import socket
import ssl
import time
import threading
import requests
from urllib3 import exceptions as ue

logger=logging.getLogger(__name__)
_retry_lock=threading.Lock()
BACKOFF_SECONDS=.25

def exception_chain(error):
    pending=[error];seen=set();result=[]
    while pending and len(result)<12:
        item=pending.pop(0)
        if not isinstance(item,BaseException) or id(item) in seen:continue
        seen.add(id(item));result.append(item)
        pending.extend([item.__cause__,item.__context__,getattr(item,'reason',None),getattr(item,'_reason',None),getattr(item,'original_error',None)])
        pending.extend(v for v in item.args if isinstance(v,BaseException))
    return result

def classify(error):
    chain=exception_chain(error)
    has=lambda types:any(isinstance(v,types) for v in chain)
    code='NETWORK_OTHER';retry=False;phase='unknown_transport'
    if has((requests.exceptions.SSLError,ssl.SSLError,ue.SSLError)):
        code='TLS_ERROR';phase='tls'
    elif has(socket.gaierror):
        code='DNS_ERROR';phase='dns'
        retry=any(isinstance(v,socket.gaierror) and v.errno==socket.EAI_AGAIN for v in chain)
    elif has((requests.ConnectTimeout,ue.ConnectTimeoutError)) and not has(ue.NewConnectionError):
        code='CONNECT_TIMEOUT';phase='connect';retry=True
    elif has((requests.ReadTimeout,ue.ReadTimeoutError)):
        code='READ_TIMEOUT';phase='read'
    elif has(http.client.RemoteDisconnected):
        code='REMOTE_DISCONNECT';phase='response'
    elif has(ConnectionResetError) or any(isinstance(v,OSError) and v.errno==errno.ECONNRESET for v in chain):
        code='CONNECTION_RESET';phase='ambiguous_send_or_read'
    elif has(ue.NewConnectionError):
        code='CONNECT_FAILURE';phase='connect'
        retry=any(isinstance(v,OSError) and v.errno in (errno.ECONNREFUSED,errno.EHOSTUNREACH,errno.ENETUNREACH) for v in chain)
    elif has((requests.Timeout,TimeoutError)):
        code='TIMEOUT_UNSPECIFIED';phase='unknown_timeout'
    safe=[]
    for item in chain:
        module=type(item).__module__
        name=type(item).__name__ if module in ('builtins','socket','ssl','http.client','requests.exceptions','urllib3.exceptions') else 'OtherError'
        row={'type':name}
        number=getattr(item,'errno',None)
        if type(number) is int:row['errno']=number
        safe.append(row)
    return dict(code=code,phase=phase,pre_send_retryable=retry,exception_chain=safe)

def identity():
    from execution_safety import CURRENT
    from ai_diagnostics import CURRENT as DIAGNOSTIC
    state=CURRENT.get() or {};job=state.get('job') or {};d=DIAGNOSTIC.get() or {}
    result={k:d[k] for k in ('trace_id','request_id') if isinstance(d.get(k),str) and len(d[k])==32 and all(v in '0123456789abcdef' for v in d[k])}
    if all(k in job for k in ('namespace','chat_id','message_id')):
        result['task_id']=hashlib.sha256(('%s:%s:%s'%(job['namespace'],job['chat_id'],job['message_id'])).encode()).hexdigest()[:16]
    return result

def observe(details,kind,attempt,elapsed_ms):
    row=dict(identity(),event='AI_TRANSPORT_FAILURE',kind=kind if kind in ('ORDER_PARSE','CONTACT_PARSE','ORDER_FLAVOR_CANDIDATE','DAILY_LEARNING','TEST_CONNECTION') else 'OTHER',attempt=attempt,http_status=None,elapsed_ms=round(elapsed_ms,3),**details)
    logger.warning('AI_TRANSPORT_FAILURE %s',json.dumps(row,sort_keys=True))

def retry_available(settings,kind,details):
    if settings.get('provider')!='DeepSeek' or kind not in ('ORDER_PARSE','CONTACT_PARSE','ORDER_FLAVOR_CANDIDATE') or not details['pre_send_retryable']:return False
    from execution_safety import CURRENT,ensure_running
    from ai_work_budget import remaining
    ensure_running();state=CURRENT.get();left=remaining()
    if left is not None and left<BACKOFF_SECONDS+1:return False
    if state:
        job=state.get('job') or {}
        if job.get('lease_until') is None or job['lease_until']<time.time()+BACKOFF_SECONDS+1:return False
        with _retry_lock:
            if state.get('network_retry_used') or state.get('technical_error'):return False
            state['network_retry_used']=True
    return True

def backoff():
    from execution_safety import ensure_running,CURRENT,ExecutionStopped
    from ai_work_budget import remaining
    ensure_running();state=CURRENT.get()
    if state:
        if state['stopped'].wait(BACKOFF_SECONDS):ensure_running()
    else:time.sleep(BACKOFF_SECONDS)
    ensure_running();left=remaining()
    if left is not None and left<=0:raise ExecutionStopped('ORDER_EXECUTION_STOPPED')
    if state and state['job'].get('lease_until',0)<=time.time():raise ExecutionStopped('ORDER_EXECUTION_STOPPED')
