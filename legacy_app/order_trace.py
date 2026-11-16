"""Per-update, bounded, privacy-safe timing; telemetry cannot change business."""
import contextvars
import functools
import logging
import time
import uuid
import asyncio
import weakref
import re
from contextlib import contextmanager

CURRENT=contextvars.ContextVar('rainbow_order_trace',default=None)
logger=logging.getLogger(__name__)

def timed_sync(name):
    def decorate(function):
        @functools.wraps(function)
        def run(*args,**kwargs):
            with stage(name):return function(*args,**kwargs)
        return run
    return decorate


def timed_async(name):
    """Measure the awaited operation, retaining the caller's trace context."""
    def decorate(function):
        @functools.wraps(function)
        async def run(*args,**kwargs):
            with stage(name):return await function(*args,**kwargs)
        return run
    return decorate

def source_event(event,namespace,chat_id,message_id,**clocks):
    """Numeric clocks and opaque source identity only; never customer content."""
    try:
        import hashlib
        source=hashlib.sha256(('%s:%s:%s'%(namespace,chat_id,message_id)).encode()).hexdigest()[:16]
        logger.info('ORDER_SOURCE_CLOCK event=%s source=%s clocks=%s',event,source,clocks)
    except Exception:pass

def commit_clock(session,execution_info):
    if not execution_info:return None
    try:
        from sqlalchemy import text
        row=session.execute(text('''SELECT source_date,received_at,started_at
            FROM telegram_order_jobs WHERE namespace=:namespace AND chat_id=:chat_id
            AND message_id=:message_id'''),execution_info).mappings().first()
        return dict(execution_info,**dict(row)) if row else None
    except Exception:return None

def committed(clocks):
    if not clocks:return
    try:
        now=time.time()
        source_event('TRANSACTION_COMMITTED',clocks['namespace'],clocks['chat_id'],clocks['message_id'],
                     telegram_at=clocks['source_date'],received_at=clocks['received_at'],
                     claimed_at=clocks['started_at'],committed_at=now)
        # received_at precedes the durable INSERT commit. This is a conservative
        # upper bound; the exact durable clock is logged at QUEUE_DURABLE.
        elapsed=(now-clocks['received_at'])*1000
        if 0<=elapsed<=86400000:mark('PERSIST_TO_COMMIT',elapsed)
    except Exception:pass

def mark(stage, elapsed_ms=0):
    record=CURRENT.get()
    if record is not None:
        record['stages'].append((stage,round(max(0,elapsed_ms),3)))

def mark_error(name):
    record=CURRENT.get()
    if record is not None:record.setdefault('errors',set()).add(name)

def _record_span(record,name,start,end):
    # Diagnostics are bounded numeric metadata, never customer text or I/O.
    try:
        if record is None:return
        label=name if isinstance(name,str) and re.fullmatch('[A-Z][A-Za-z_0-9]{0,63}',name) else 'OTHER'
        spans=record.setdefault('spans',[])
        if len(spans)>=128:
            record['spans_dropped']=record.get('spans_dropped',0)+1;return
        origin=record['started']
        spans.append((label,round(max(0,start-origin)*1000,3),round(max(0,end-origin)*1000,3)))
    except Exception:
        pass

@contextmanager
def stage(name):
    start=time.perf_counter()
    record=CURRENT.get()
    try:
        yield
    except BaseException:
        mark_error(name)
        raise
    finally:
        mark(name,(time.perf_counter()-start)*1000)
        _record_span(record,name,start,time.perf_counter())

def first_response():
    record=CURRENT.get()
    if record is not None and record['ttfr_ms'] is None:
        record['ttfr_ms']=round((time.perf_counter()-record['started'])*1000,3)
        logger.info('ORDER_FIRST_RESPONSE source=%s elapsed_ms=%.3f',record.get('source','none'),record['ttfr_ms'])


def bind_receipt_source(database,chat_id,message_id):
    """Original customer clock, including replies/callbacks on an older draft."""
    record=CURRENT.get()
    if record is None:return
    try:
        from execution_safety import CURRENT as EXECUTION
        from telegram_source_identity import scope
        import hashlib
        record['receipt_chat_sha256']=hashlib.sha256(str(chat_id).encode()).hexdigest()
        job=(EXECUTION.get() or {}).get('job') or {}
        row=job if str(job.get('chat_id'))==str(chat_id) and job.get('message_id')==message_id else None
        namespace=scope(chat_id)
        if database is not None:
            from sqlalchemy import text
            with database.engine.connect() as c:
                found=c.execute(text('''SELECT source_date,received_at,started_at FROM telegram_order_jobs
                    WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message'''),
                    dict(namespace=namespace,chat=str(chat_id),message=message_id)).mappings().first()
                if found:row=dict(found)
        if row:
            import hashlib
            record.update(receipt_source=hashlib.sha256(f'{namespace}:{chat_id}:{message_id}'.encode()).hexdigest()[:16],
                          receipt_source_at=row.get('source_date'),receipt_received_at=row.get('received_at'),
                          receipt_message_id=message_id)
    except Exception:pass


def receipt_plan(parts):
    record=CURRENT.get()
    if record is not None:
        record['receipt_parts']=parts;record['receipt_acks']=[]


def receipt_ack(message_id,ack_at,chat_sha256=None):
    """Network ACK observed before audit fsync. Only the final chunk completes."""
    record=CURRENT.get()
    if record is None or not record.get('receipt_parts'):return
    try:
        acks=record['receipt_acks'];acks.append(message_id)
        if record.get('receipt_chat_sha256') and chat_sha256!=record['receipt_chat_sha256']:
            record['receipt_destination_mismatch']=True
        complete=len(acks)==record['receipt_parts'] and not record.get('receipt_destination_mismatch')
        if complete:record['receipt_complete_ack_at']=ack_at
        source_at=record.get('receipt_source_at')
        elapsed=(ack_at-source_at)*1000 if isinstance(source_at,(int,float)) else None
        if complete and elapsed is not None and elapsed>=0:mark('ORDER_TO_RECEIPT_ACK',elapsed)
        logger.info('ORDER_RECEIPT_ACK source=%s ack_at=%.6f source_at=%s received_at=%s message_id=%s part=%d parts=%d complete=%s source_to_ack_ms=%s',
                    record.get('receipt_source',record.get('source','none')),ack_at,source_at,
                    record.get('receipt_received_at'),message_id,len(acks),record['receipt_parts'],complete,elapsed)
    except Exception:pass

def persist(record,total_ms,stage_name='COMPLETE_RECEIPT',error=False):
    from performance_metrics import record_samples,mode,STAGES
    def provider(stage):
        return 'AliyunAddress' if stage.startswith('ADDRESS_') else 'Local'
    identity=dict(trace_id=record.get('trace_id'),source=record.get('receipt_source',record.get('source')))
    samples=[dict(identity,mode=mode(),provider=provider(name),stage=name,elapsed_ms=elapsed,error=name in record.get('errors',set())) for name,elapsed in record['stages'] if name in STAGES]
    samples.append(dict(identity,mode=mode(),provider=provider(stage_name),stage=stage_name,elapsed_ms=total_ms,error=error))
    # Only a successful API whose post-commit clock was recorded opts in.
    # Other API results and Bot/Worker telemetry retain their original path.
    if stage_name=='API_TOTAL' and not error and any(name=='PERSIST_TO_COMMIT' for name,_ in record['stages']):
        record_samples(samples,committed_api=True)
    else:
        record_samples(samples)

class Message:
    def __init__(self,original):self.original=original
    def __getattr__(self,name):return getattr(self.original,name)
    async def reply_text(self,*args,**kwargs):
        from execution_safety import ensure_customer_validation
        ensure_customer_validation()
        with stage('TELEGRAM_SEND'):
            result=await self.original.reply_text(*args,**kwargs)
        first_response()
        return result

class Update:
    def __init__(self,original):
        self.original=original
        self.message=Message(original.message) if getattr(original,'message',None) else None
        self.effective_message=self.message or getattr(original,'effective_message',None)
    def __getattr__(self,name):return getattr(self.original,name)

def traced(function):
    @functools.wraps(function)
    async def run(self,update,context,*args,**kwargs):
        if CURRENT.get() is not None:
            return await function(self,update,context,*args,**kwargs)
        record={'trace_id':uuid.uuid4().hex,'started':time.perf_counter(),'started_wall':time.time(),'ttfr_ms':None,'stages':[]}
        from execution_safety import CURRENT as EXECUTION
        job=(EXECUTION.get() or {}).get('job') or {}
        if all(k in job for k in ('namespace','chat_id','message_id')):
            import hashlib
            record['source']=hashlib.sha256(('%s:%s:%s'%(job['namespace'],job['chat_id'],job['message_id'])).encode()).hexdigest()[:16]
        token=CURRENT.set(record)
        failed=False
        try:
            from rule_dictionary import runtime_snapshot
            from request_memo import request_memo_scope
            from ai_work_budget import work_budget
            with runtime_snapshot(), request_memo_scope(), work_budget():
                return await function(self,Update(update),context,*args,**kwargs)
        except BaseException:
            failed=True
            raise
        finally:
            try:
                processing_ms=(time.perf_counter()-record['started'])*1000
                telemetry_started=time.perf_counter()
                await asyncio.to_thread(persist,record,processing_ms,error=failed)
                logger.info('ORDER_TRACE trace_id=%s duration_ms=%.3f ttfr_ms=%s source=%s processing_ms=%.3f telemetry_ms=%.3f stages=%s',
                    record['trace_id'],(time.perf_counter()-record['started'])*1000,
                    record['ttfr_ms'],record.get('source','none'),processing_ms,
                    (time.perf_counter()-telemetry_started)*1000,record['stages'][-64:])
                logger.info('ORDER_SPANS trace_id=%s started_at=%s spans=%s spans_dropped=%s',
                    record['trace_id'],record['started_wall'],record.get('spans',[]),record.get('spans_dropped',0))
            except Exception:
                # Diagnostics must never change a committed business outcome.
                pass
            finally:
                CURRENT.reset(token)
    return run


def api_traced(function):
    """Correlate Bot and transactional API timing without persisting customer text."""
    @functools.wraps(function)
    async def run(*args, **kwargs):
        import re
        requested=kwargs.get('diagnostic_trace_id')
        trace_id=requested if isinstance(requested,str) and re.fullmatch(r'[a-f0-9]{32}',requested) else uuid.uuid4().hex
        record={'trace_id':trace_id,'started':time.perf_counter(),'started_wall':time.time(),'ttfr_ms':None,'stages':[]}
        token=CURRENT.set(record)
        failed=False
        try:
            from request_memo import request_memo_scope
            from rule_dictionary import runtime_snapshot
            # One consistent dictionary read per API request, including the
            # database worker reached through to_thread. Never cross-request.
            with runtime_snapshot(), request_memo_scope():
                return await function(*args,**kwargs)
        except BaseException:
            failed=True
            raise
        finally:
            try:
                logger.info('ORDER_API_TRACE trace_id=%s duration_ms=%.3f stages=%s',
                    trace_id,(time.perf_counter()-record['started'])*1000,record['stages'][-64:])
                logger.info('ORDER_API_SPANS trace_id=%s started_at=%s spans=%s spans_dropped=%s',
                    trace_id,record['started_wall'],record.get('spans',[]),record.get('spans_dropped',0))
                await asyncio.to_thread(persist,record,(time.perf_counter()-record['started'])*1000,'API_TOTAL',failed)
            except Exception:
                pass
            finally:
                CURRENT.reset(token)
    return run


from telegram.ext import BaseUpdateProcessor

class ScopedUpdateProcessor(BaseUpdateProcessor):
    """Parallel customers; serialized sender/chat flows including callbacks."""
    def __init__(self, max_concurrent_updates=32):
        super().__init__(max_concurrent_updates)
        self._locks=weakref.WeakValueDictionary()

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_process_update(self, update, coroutine):
        chat=getattr(getattr(update,'effective_chat',None),'id',None)
        sender=getattr(getattr(update,'effective_user',None),'id',None)
        key=(chat,sender)
        lock=self._locks.get(key)
        if lock is None:
            lock=asyncio.Lock();self._locks[key]=lock
        async with lock:
            await coroutine
