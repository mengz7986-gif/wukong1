"""Append-only delivery evidence, NOT an outbox or a recovery sender.

Unknown results must never be replayed by this module. No recipient text,
credentials, customer IDs or addresses are stored.
"""
import asyncio,fcntl,hashlib,json,os,re,time,uuid
from pathlib import Path

def append(row):
    path=Path(os.getenv('RAINBOW_RECEIPT_AUDIT_PATH','success_receipt_audit.jsonl'))
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'a') as stream:
        os.fchmod(stream.fileno(),0o600);fcntl.flock(stream,fcntl.LOCK_EX)
        stream.write(json.dumps(dict(row,at=time.time()),sort_keys=True)+'\n')
        stream.flush();os.fsync(stream.fileno())

def identity(facts):
    from execution_safety import CURRENT
    job=(CURRENT.get() or {}).get('job') or {}
    values=facts.get('order_numbers') or [facts.get('order_number')]
    if isinstance(values,str):values=[values]
    numbers=[str(n) for n in values if re.fullmatch(r'[A-Za-z0-9_-]{1,64}',str(n or ''))]
    task=None
    if all(k in job for k in ('namespace','chat_id','message_id')):
        task=hashlib.sha256(('%s:%s:%s'%(job['namespace'],job['chat_id'],job['message_id'])).encode()).hexdigest()[:16]
    result=dict(order_numbers=numbers,task_id=task,outbox_event_key=None,delivery_path='DIRECT_BOT')
    # Provenance only, never store the temporary label or customer PII here.
    if facts.get('_recipient_generated'):
        result['recipient_source']='ADMIN_TEMPORARY'
    return result

async def finish(row):
    # Never turn an acknowledged send into a recovery send because telemetry
    # failed afterwards. The durable SENDING entry then stays UNKNOWN.
    try:await asyncio.to_thread(append,row)
    except Exception:
        import logging
        logging.getLogger(__name__).error('RECEIPT_AUDIT_FINALIZE_UNAVAILABLE')

def transport_error(error):
    """Bounded type-only cause metadata; never stringify API/SQL exceptions."""
    known={'TimedOut','NetworkError','RetryAfter','BadRequest','Forbidden','CancelledError',
           'ConnectTimeout','ReadTimeout','WriteTimeout','PoolTimeout','ConnectError',
           'ReadError','WriteError','RemoteProtocolError','LocalProtocolError','DeliveryQueueFull'}
    phases={'ConnectTimeout':'CONNECT','ConnectError':'CONNECT','ReadTimeout':'READ',
            'ReadError':'READ','WriteTimeout':'WRITE','WriteError':'WRITE','PoolTimeout':'POOL',
            'BadRequest':'API_REJECTED','Forbidden':'API_REJECTED','RetryAfter':'RATE_LIMITED',
            'CancelledError':'CANCELLED'}
    chain=[];seen=set();current=error
    while current is not None and len(chain)<6 and id(current) not in seen:
        seen.add(id(current));name=type(current).__name__
        chain.append(name if name in known else 'OTHER')
        current=current.__cause__ or current.__context__
    cause=next((x for x in chain[1:] if x in phases),None)
    return dict(exception_kind=chain[0],cause_kind=cause or (chain[1] if len(chain)>1 else None),
                transport_phase=phases.get(cause or chain[0],'OUTCOME_UNKNOWN'))

def intent_metadata(target,method,args,kwargs):
    from order_trace import CURRENT
    trace=CURRENT.get() or {};chat=kwargs.get('chat_id',getattr(target,'chat_id',None))
    if chat is None and method=='send_message' and args:chat=args[0]
    result={}
    if isinstance(chat,(str,int)) and str(chat).lstrip('-').isdigit():
        result['intended_destination_sha256']=hashlib.sha256(str(chat).encode()).hexdigest()
    for key in ('message_thread_id','reply_to_message_id'):
        value=kwargs.get(key)
        if type(value) is int:result[key]=value
    if type(trace.get('receipt_parts')) is int:
        result.update(part_index=len(trace.get('receipt_acks',[]))+1,part_count=trace['receipt_parts'])
    client=target.get_bot() if method=='edit_text' and hasattr(target,'get_bot') else target
    timeout=getattr(getattr(getattr(client,'request',None),'_client',None),'timeout',None)
    configured={}
    for name in ('connect','read','write','pool'):
        value=kwargs.get(name+'_timeout',getattr(timeout,name,None))
        if isinstance(value,(int,float)):configured[name]=float(value)
    if configured:result['timeout_seconds']=configured
    return result

def assert_quiet():
    """Fail closed; exact user-approved evidence reviews never alter UNKNOWN."""
    path=Path(os.getenv('RAINBOW_RECEIPT_AUDIT_PATH','success_receipt_audit.jsonl'))
    production_history=os.environ.get('DATABASE_URL','').startswith('sqlite:////var/lib/newbot/live/')
    if not path.exists():
        if production_history:raise RuntimeError('SIX_HOLD_HISTORY_REQUIRED')
        return
    if path.stat().st_size>32*1024*1024:raise RuntimeError('RECEIPT_AUDIT_REVIEW_REQUIRED')
    latest={}
    try:
        with path.open() as stream:
            fcntl.flock(stream,fcntl.LOCK_SH|fcntl.LOCK_NB)
            for line in stream:
                row=json.loads(line);attempt=row['attempt_id'];status=row['status']
                if status not in ('SENDING','SENT','NOT_SENT','UNKNOWN'):raise ValueError()
                if status=='SENT' and (type(row.get('message_id')) is not int or row['message_id']<=0):raise ValueError()
                if attempt in latest and latest[attempt]['status']!='SENDING':raise ValueError()
                if attempt not in latest and status!='SENDING':raise ValueError()
                latest[attempt]=row
    except (OSError,ValueError,KeyError,TypeError):raise RuntimeError('RECEIPT_AUDIT_INVALID_OR_BUSY') from None
    unresolved=[row for row in latest.values() if row['status'] in ('SENDING','UNKNOWN')]
    if unresolved or production_history:
        try:
            from receipt_review_gate import reviewed_unresolved
            reviewed=reviewed_unresolved(unresolved)
            if set(reviewed)!={r['attempt_id'] for r in unresolved}:raise ValueError()
        except Exception:raise RuntimeError('RECEIPT_AUDIT_UNKNOWN_SEND') from None

def records_for_order(order_number):
    """Read-only admin lookup; absence is unknown, never proof of no send."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',str(order_number or '')):raise ValueError('RECEIPT_ORDER_NUMBER_INVALID')
    path=Path(os.getenv('RAINBOW_RECEIPT_AUDIT_PATH','success_receipt_audit.jsonl'))
    if not path.exists():return []
    if path.stat().st_size>32*1024*1024:raise ValueError('RECEIPT_AUDIT_REVIEW_REQUIRED')
    latest={}
    with path.open() as stream:
        fcntl.flock(stream,fcntl.LOCK_SH|fcntl.LOCK_NB)
        for line in stream:
            row=json.loads(line)
            if order_number in row.get('order_numbers',[]):latest[row['attempt_id']]=row
    result=[]
    for row in latest.values():
        row=dict(row)
        if row.get('status')=='SENDING':row['status']='UNKNOWN'
        result.append(row)
    return result

def records_for_orders(order_numbers):
    """One bounded snapshot per admin page; release file lock before parsing."""
    wanted=set(order_numbers)
    if len(wanted)>200:raise ValueError('RECEIPT_QUERY_LIMIT')
    result={n:[] for n in wanted}
    path=Path(os.getenv('RAINBOW_RECEIPT_AUDIT_PATH','success_receipt_audit.jsonl'))
    if not path.exists():return result
    with path.open('rb') as stream:
        fcntl.flock(stream,fcntl.LOCK_SH|fcntl.LOCK_NB)
        data=stream.read(32*1024*1024+1)
    if len(data)>32*1024*1024:raise ValueError('RECEIPT_QUERY_TOO_LARGE')
    latest={}
    for line in data.splitlines():
        row=json.loads(line)
        if wanted.intersection(row.get('order_numbers',[])):latest[row['attempt_id']]=row
    for row in latest.values():
        row=dict(row)
        if row['status']=='SENDING':row['status']='UNKNOWN'
        for n in wanted.intersection(row.get('order_numbers',[])):result[n].append(row)
    return result

def delivery_summary(attempts,outbox=()):
    """Delivery state only; never reinterpret an order's business status."""
    states={x.get('status') for x in attempts}
    queued={x.get('status') for x in outbox}
    if states & {'UNKNOWN','SENDING'} or queued & {'unknown','sending'}:return 'UNKNOWN'
    if 'pending' in queued:return 'PENDING'
    # New per-part evidence allows a rejected edit followed by an ACK without
    # mistaking a multipart partial ACK for a complete successful receipt.
    planned=[x for x in attempts if type(x.get('part_index')) is int and type(x.get('part_count')) is int]
    if planned and len(planned)==len(attempts):
        latest={}
        for x in sorted(planned,key=lambda x:x.get('at',0)):latest[x['part_index']]=x
        counts={x['part_count'] for x in planned}
        if len(counts)!=1:return 'UNKNOWN'
        count=counts.pop()
        if count<1 or count>200:return 'UNKNOWN'
        if set(latest)==set(range(1,count+1)) and all(x['status']=='SENT' for x in latest.values()):return 'SENT'
        if any(x['status']=='SENT' for x in latest.values()):return 'PARTIAL'
    if states=={'SENT'} or not states and queued=={'sent'}:return 'SENT'
    if states=={'NOT_SENT'}:return 'NOT_SENT'
    return 'UNKNOWN' if states else 'NO_EVIDENCE'

class ReceiptSender:
    def __init__(self,target,facts):
        self._target=target;self._facts=identity(facts)
        self._order_id=facts.get('id') or facts.get('order_id')
        self._receipt=bool(facts.get('_automatic_receipt') or facts.get('_group_receipt'))
    def __getattr__(self,name):return getattr(self._target,name)
    async def _call(self,method,*args,**kwargs):
        from six_unknown_receipt_hold import guard_direct
        guard_direct(self._facts,order_id=self._order_id)
        evidence=dict(self._facts,attempt_id=uuid.uuid4().hex,method=method,
                      **intent_metadata(self._target,method,args,kwargs))
        from order_trace import stage
        with stage('RECEIPT_AUDIT_INTENT'):
            await asyncio.to_thread(append,dict(evidence,status='SENDING'))
        target=self._target
        if method=='edit_text' and hasattr(target,'get_bot'):target=target.get_bot()
        if self._receipt and method!='edit_text' and hasattr(target,'rate_limiter'):
            kwargs['rate_limit_args']=dict(kwargs.get('rate_limit_args') or {},order_receipt=True)
        try:result=await getattr(self._target,method)(*args,**kwargs)
        except BaseException as error:
            from telegram.error import BadRequest,Forbidden
            unchanged=isinstance(error,BadRequest) and 'message is not modified' in str(error).lower()
            status='SENT' if unchanged else 'NOT_SENT' if isinstance(error,(BadRequest,Forbidden)) else 'UNKNOWN'
            mid=(kwargs.get('message_id') or getattr(self._target,'message_id',None)) if unchanged else None
            await finish(dict(evidence,status=status,message_id=mid,
                result_kind='UNCHANGED_EXISTING_MESSAGE' if unchanged else 'REJECTED' if status=='NOT_SENT' else 'AMBIGUOUS',
                **transport_error(error)))
            raise
        ack_at=time.time()
        mid=getattr(result,'message_id',None)
        if type(mid) is not int or mid<=0:
            mid=(kwargs.get('message_id') or getattr(self._target,'message_id',None)) if result is True else None
        status='SENT' if type(mid) is int and mid>0 else 'UNKNOWN'
        chat=getattr(result,'chat',None)
        destination=getattr(chat,'id',None)
        destination_sha256=hashlib.sha256(str(destination).encode()).hexdigest() if destination is not None else None
        if status=='SENT':
            from order_trace import receipt_ack
            receipt_ack(mid,ack_at,destination_sha256)
        with stage('RECEIPT_AUDIT_FINALIZE'):
            await finish(dict(evidence,status=status,message_id=mid if status=='SENT' else None,
                              ack_at=ack_at if status=='SENT' else None,destination_sha256=destination_sha256,
                              destination_type=getattr(chat,'type',None),result_kind='ACK' if status=='SENT' else 'ACK_WITHOUT_MESSAGE_ID',
                              transport_phase='ACK' if status=='SENT' else 'ACK_ID_MISSING'))
        return result
    async def send_message(self,*args,**kwargs):return await self._call('send_message',*args,**kwargs)
    async def edit_message_text(self,*args,**kwargs):return await self._call('edit_message_text',*args,**kwargs)
    async def edit_text(self,*args,**kwargs):
        # Message.edit_text has its target ID on the object rather than kwargs.
        return await self._call('edit_text',*args,**kwargs)
