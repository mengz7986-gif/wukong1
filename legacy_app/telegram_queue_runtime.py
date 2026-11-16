"""Bounded durable worker adapter; ingress never waits for an AI request."""
import asyncio
import logging
import json
import traceback
from pathlib import Path
from telegram import Update
from telegram.ext import CallbackContext
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)


from order_trace import traced

@traced
async def _traced_callback(owner,update,context):
    from telegram_callback_runtime import dispatch
    return await dispatch(owner,update,context)


def safe_failure_details(error):
    """Never stringify SQLAlchemy errors: SQL parameters can contain PII."""
    original = getattr(error, 'orig', error)
    code = getattr(original, 'sqlite_errorcode', None)
    sqlite_name = getattr(original, 'sqlite_errorname', None)
    telegram_reason=None
    from telegram.error import BadRequest
    if isinstance(error,BadRequest):
        # Classify fixed API error phrases only. Never emit arbitrary Telegram
        # descriptions (which can echo message content), SQL or parameters.
        description=str(error).casefold()
        patterns={
            'REPLY_TARGET_MISSING':('message to be replied not found','replied message not found'),
            'CHAT_NOT_FOUND':('chat not found',),
            'SEND_PERMISSION_DENIED':('not enough rights','have no rights to send'),
            'ENTITY_FORMAT_INVALID':("can't parse entities",'cannot parse entities'),
            'MESSAGE_TOO_LONG':('message is too long',),
            'MESSAGE_NOT_MODIFIED':('message is not modified',),
            'CALLBACK_EXPIRED':('query is too old','query id is invalid'),
            'THREAD_NOT_FOUND':('message thread not found',),
        }
        telegram_reason=next((key for key,phrases in patterns.items()
                             if any(phrase in description for phrase in phrases)), 'OTHER_BAD_REQUEST')
    return dict(kind=type(error).__name__, original_kind=type(original).__name__, telegram_reason=telegram_reason,
        sqlite_code=code if isinstance(code, int) else None,
        sqlite_name=sqlite_name if isinstance(sqlite_name, str) and sqlite_name.startswith('SQLITE_') else None,
        frames=[dict(file=Path(frame.filename).name, line=frame.lineno,
            function=frame.name) for frame in traceback.extract_tb(error.__traceback__)[-12:]])


class IngressQueue(asyncio.Queue):
    """Persist before PTB polling advances its Telegram update offset."""
    runtime=None

    @staticmethod
    def _maintenance_paused():
        # Existing admission file only: never create locks from ingress.
        import os,fcntl
        from deployment_drain import path,_read
        fd=os.open(str(path())+'.admission.lock',os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'r') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
            except BlockingIOError:return True
            return _read()['paused']

    async def put(self,item):
        if isinstance(item,Update):
            from newbot_safety import update_allowed
            if not update_allowed(item):return
        if isinstance(item,Update) and self.runtime is not None:
            message=item.message
            if item.callback_query or (message and (message.text or message.caption)):
                attempts=0
                while True:
                    try:
                        await self.runtime.receive(item,None)
                        break
                    except OperationalError as error:
                        # A transient SQLite writer lock must not kill PTB's
                        # polling task. Keep this exact update unacknowledged
                        # until durable receipt succeeds; no business POST retry.
                        original=getattr(error,'orig',None)
                        code=getattr(original,'sqlite_errorcode',None)
                        message=str(original).lower()
                        if code not in (5,6) and message not in (
                                'database is locked','database table is locked'):
                            raise
                        attempts+=1
                        logger.warning('INGRESS_PERSIST_RETRY kind=SQLITE_BUSY attempts=%s',attempts)
                        await asyncio.sleep(min(2.0,.1*2**min(attempts-1,5)))
        if (isinstance(item,Update) and self.runtime is not None
                and getattr(self.runtime,'p0_protection',None) is not None):
            # Persist above, but do not hand direct commands such as /stock to
            # PTB while maintenance is paused. PTB advances the polling offset
            # only after put() finishes, so cancellation cannot lose commands.
            while self._maintenance_paused():
                await asyncio.sleep(.05)
        await super().put(item)


class Runtime:
    def __init__(self, owner, queue, application, workers=4, callback_workers=2):
        if not 1 <= workers <= 16 or not 0 <= callback_workers <= 4:
            raise ValueError('队列并发必须有界')
        self.owner, self.queue, self.application = owner, queue, application
        # A flooded chat must not occupy every recognition worker while its
        # receipts wait for per-chat Telegram pacing. Callbacks retain their
        # separate reserved lane and original actor/source ordering fences.
        self.queue.message_chat_limit=max(1,(workers+1)//2)
        import inspect
        self._claim_accepts_lane='lane' in inspect.signature(queue.claim).parameters
        self.stopping = asyncio.Event()
        self.last_reconciled=0
        import os
        self.processing_timeout=max(60,min(600,int(os.getenv('ORDER_PROCESSING_TIMEOUT_SECONDS','300'))))
        self.queue_max_wait=max(300,min(3600,int(os.getenv('ORDER_QUEUE_MAX_WAIT_SECONDS','900'))))
        self.executions={}
        self.uncertain_messages={}
        self.receipts_recovered=False
        from p0_recovery_protection import attach, Protection, DEFAULT_PATH
        guard=getattr(owner,'_newbot_instance_guard',None)
        if guard is not None and os.getenv('NEWBOT_INGESTION_POLICY_PATH')=='/etc/newbot/ingestion-policy.json':
            hold=Protection(DEFAULT_PATH)
            recovery=self.queue.recover_after_restart(guard,after=hold.policy['cutoff_received_at'],protected=hold.entries)
            logger.info('ORDER_RESTART_RECOVERY requeued=%d committed_observed=%d receipts_replayed=0',
                        recovery['requeued'],recovery['committed_observed'])
        self.p0_protection=attach(self.queue)
        self._p0_maintenance_offset=0
        self.queue.expiry_requires_fence=True
        if isinstance(application.update_queue,IngressQueue):
            application.update_queue.runtime=self
        # Zero reserved slots retains legacy shared-worker behavior, useful for
        # bounded deployments and comparisons; callbacks must not starve forever.
        self.tasks = [asyncio.create_task(self.worker('message' if callback_workers else None)) for _ in range(workers)]
        self.tasks += [asyncio.create_task(self.worker('callback')) for _ in range(callback_workers)]
        self.maintenance_task=asyncio.create_task(self.maintain())

    async def guarded_handler(self,job,update,context,is_callback):
        from execution_safety import execution
        key=(job['namespace'],job['chat_id'],job['message_id'])
        with execution(job) as value:
            self.executions[key]=value
            try:
                if is_callback:
                    return await _traced_callback(self.owner,update,context)
                return await self.owner.handle_message(update,context)
            finally:
                if value.get('technical_error'):
                    context._rainbow_technical_error=value['technical_error']
                value['stopped'].set()
                self.executions.pop(key,None)

    async def maintain(self):
        from execution_safety import resolve,deliver,recover_dispatches
        import time
        while not self.stopping.is_set():
            await asyncio.sleep(2)
            from deployment_drain import begin_activity,end_activity,effective_wait_age,snapshot
            activity=begin_activity()
            if activity is None:continue
            try:
                if not self.receipts_recovered:
                    await asyncio.to_thread(recover_dispatches,self.queue.database)
                    self.receipts_recovered=True
                now=time.time()
                from telegram_source_identity import scope,account_cutover
                boundary=account_cutover()
                def candidates():
                    with self.queue.database.engine.connect() as connection:
                        return [dict(row) for row in connection.execute(text('''SELECT * FROM telegram_order_jobs
                          WHERE namespace IN ('shared',:observer)
                          AND (:boundary IS NULL OR received_at>=:boundary)
                          AND reason IS NOT 'BOT_ACCOUNT_CUTOVER_HISTORICAL_UPDATE'
                          AND ((state='PROCESSING' AND lease_until<:now)
                          OR state IN ('FAILED_RETRYABLE','REVIEW_REQUIRED')
                          OR (state='RECEIVED' AND received_at<:cutoff))
                          ORDER BY received_at,namespace,chat_id,message_id LIMIT 8 OFFSET :p0_offset'''),dict(p0_offset=self._p0_maintenance_offset if self.p0_protection is not None else 0,now=now,cutoff=now-self.queue_max_wait,
                          observer=scope(1),boundary=boundary.timestamp() if boundary else None)).mappings()]
                candidate_jobs=await asyncio.to_thread(candidates)
                if self.p0_protection is not None:
                    self._p0_maintenance_offset=(self._p0_maintenance_offset+len(candidate_jobs)) if len(candidate_jobs)==8 else 0
                for job in candidate_jobs:
                    if self.p0_protection is not None and self.p0_protection.blocks(job,self.queue.cipher):
                        continue
                    if job['state']=='RECEIVED' and effective_wait_age(job['received_at'],now)<self.queue_max_wait:continue
                    key=(job['namespace'],job['chat_id'],job['message_id'])
                    value=self.executions.get(key)
                    if self.uncertain_messages.get(key):job['checking_message_id']=self.uncertain_messages[key]
                    resolved=await asyncio.to_thread(resolve,self.queue.database,job,'ORDER_TIMEOUT',
                        only_if_lease_expired=job['state']=='PROCESSING')
                    if resolved and value:value['stopped'].set()
                if getattr(self.application.bot,'_initialized',False):
                    await deliver(self.queue.database,self.application.bot)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning('EXECUTION_SAFETY_MAINTENANCE kind=%s',type(error).__name__)
            finally:end_activity(activity)

    async def uncertain_notice(self,job):
        if job['message_id']<=0 or not getattr(self.application.bot,'_initialized',False):return
        key=(job['namespace'],job['chat_id'],job['message_id'])
        if key in self.uncertain_messages:return
        self.uncertain_messages[key]=None
        try:
            message=await asyncio.wait_for(self.application.bot.send_message(
                chat_id=job['chat_id'],reply_to_message_id=job['message_id'],
                text='⚠️ 订单状态核查中\n\n本次订单处理出现异常，系统正在核对是否已经录入。\n\n请暂时不要重复发送相同订单，避免重复录入。\n\n核查完成后会更新处理结果。'),timeout=15)
            self.uncertain_messages[key]=message.message_id
        except Exception as error:
            logger.warning('EXECUTION_CHECK_NOTICE_UNCERTAIN kind=%s',type(error).__name__)

    async def receive(self, update, context):
        from newbot_safety import update_allowed
        if not update_allowed(update):return
        message = getattr(update, 'message', None)
        sender = getattr(update, 'effective_user', None)
        callback=getattr(update,'callback_query',None)
        if callback and sender and not sender.is_bot:
            from telegram_callback_runtime import acknowledge, trace
            logger.info('CALLBACK_PHASE phase=receive %s update_id=%s',trace(callback),update.update_id)
            await acknowledge(callback)
            if not callback.message:
                logger.warning('CALLBACK_PHASE phase=unsupported_inline')
                return
            # Real message IDs are positive. Telegram update ID supplies a
            # collision-free negative operational key for callback events.
            minimal={'update_id':update.update_id,'callback_query':callback.to_dict()}
            inserted=await asyncio.to_thread(self.queue.receive,callback.message.chat_id,
                -(int(update.update_id)+1),sender.id,minimal)
            logger.info('CALLBACK_PHASE phase=queue %s update_id=%s inserted=%s',trace(callback),update.update_id,inserted)
            return
        if not message or not sender or sender.is_bot:
            return
        content = message.text or message.caption
        if not content:
            return
        if message.caption and not self.owner._looks_like_order_candidate(content):
            return
        if message.text and any(e.type=='bot_command' and e.offset==0 for e in (message.entities or ())):
            return
        # Media is never downloaded. Replay only the caption as ordinary text.
        minimal = {'update_id': update.update_id, 'message': {
            'message_id': message.message_id, 'date': int(message.date.timestamp()),
            'chat': message.chat.to_dict(), 'from': sender.to_dict(), 'text': content}}
        if message.reply_to_message:
            reply = message.reply_to_message
            minimal['message']['reply_to_message'] = {
                'message_id': reply.message_id, 'date': int(reply.date.timestamp()),
                'chat': reply.chat.to_dict(), 'from': reply.from_user.to_dict() if reply.from_user else sender.to_dict()}
        await asyncio.to_thread(self.queue.receive, message.chat_id,
                                message.message_id, sender.id, minimal)

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(20)
            if not await asyncio.to_thread(self.queue.renew, job):
                raise RuntimeError('ORDER_JOB_LEASE_LOST')

    def outcome(self, job):
        # Business facts win, including a crash after commit but before receipt.
        with self.queue.database.engine.connect() as c:
            from telegram_source_identity import source_order_exists
            completed = source_order_exists(c,job['namespace'],job['chat_id'],job['message_id'])
            if completed:
                return 'COMPLETED'
            pending = c.execute(text('''SELECT d.status FROM telegram_message_events e
                JOIN telegram_order_drafts d ON d.token=e.draft_token
                WHERE e.namespace=:namespace AND d.namespace=e.namespace AND e.chat_id=:chat AND e.message_id=:message
                AND e.sender_id=:sender'''),
                {'namespace':job['namespace'],'chat': job['chat_id'], 'message': job['message_id'], 'sender': job['sender_id']}).scalar()
            return 'NEED_CONFIRMATION' if pending == 'pending' else 'REJECTED'

    def recoverable_outcome(self, job):
        """Committed facts can recover a failed handler without replaying it.

        Minimal ACK-only drafts are not proof of completed processing. Only an
        owned actionable draft with a persisted prompt permits continuation.
        Callback partial changes never qualify: their processing must resume.
        """
        if job['decoded_payload'].get('callback_query'):
            return None
        if self.outcome(job) == 'COMPLETED':
            return 'COMPLETED'
        with self.queue.database.engine.connect() as c:
            row=c.execute(text('''SELECT d.payload FROM telegram_message_events e
                JOIN telegram_order_drafts d ON d.token=e.draft_token
                WHERE e.namespace=:namespace AND d.namespace=e.namespace
                AND e.chat_id=:chat AND e.message_id=:message AND e.sender_id=:sender
                AND d.message_id=:message
                AND d.sender_id=:sender AND d.user_id=e.user_id AND d.status='pending' '''),
                dict(namespace=job['namespace'], chat=job['chat_id'],
                     message=job['message_id'], sender=job['sender_id'])).scalar()
        if not row:
            return None
        payload=json.loads(row)
        prompts=payload.get('_reply_message_ids')
        if not isinstance(prompts,list) or not any(type(n) is int and n>0 for n in prompts):
            return None
        for key in ('pending_ai_flavor','pending_recipient_order','pending_order','pending_manual_review'):
            value=payload.get(key)
            if not isinstance(value,dict):
                continue
            ready=(
                key=='pending_recipient_order' and isinstance(value.get('order_info'),dict)
                and bool(value['order_info'].get('items'))
                or key=='pending_ai_flavor' and isinstance(value.get('result'),dict)
                and isinstance(value.get('proposal'),dict) and bool(value['proposal'].get('choices'))
                or key=='pending_order' and bool(value.get('items'))
                or key=='pending_manual_review' and bool(value.get('raw_text'))
                and isinstance(value.get('reasons'),list))
            if ready:
                return 'NEED_CONFIRMATION'
        return None

    async def worker(self, lane=None):
        import time
        if not hasattr(self,'executions'):self.executions={}
        while not self.stopping.is_set():
            job = None
            heartbeat = None
            update = None
            context = None
            phase = 'reconcile'
            try:
                now=asyncio.get_running_loop().time()
                if now-self.last_reconciled>=5:
                    self.last_reconciled=now
                    await asyncio.to_thread(self.queue.reconcile_confirmations)
                phase = 'claim'
                claim_started=time.monotonic()
                claim_wall=time.time()
                if lane is None or not self._claim_accepts_lane:
                    job = await asyncio.to_thread(self.queue.claim)
                else:
                    job = await asyncio.to_thread(self.queue.claim, lane=lane)
                claim_ms=max(0,time.monotonic()-claim_started)*1000
                if job is None:
                    await asyncio.sleep(.1)
                    continue
                heartbeat = asyncio.create_task(self.heartbeat(job))
                import time, hashlib
                source_tag=hashlib.sha256(('%s:%s:%s' % (job['namespace'],job['chat_id'],job['message_id'])).encode()).hexdigest()[:16]
                logger.info('ORDER_QUEUE_PHASE phase=start source=%s lane=%s wait_ms=%d attempt=%d',
                    source_tag,lane,int(max(0,time.time()-job['received_at'])*1000),int(job.get('attempts') or 0)+1)
                processing_started=time.monotonic()
                from performance_metrics import record_samples,mode
                is_callback=bool(job['decoded_payload'].get('callback_query'))
                queue_metrics=[] if is_callback else [dict(mode=mode(),stage='MESSAGE_QUEUE',elapsed_ms=max(0,claim_wall-job['received_at'])*1000),dict(mode=mode(),stage='WORKER_CLAIM',elapsed_ms=claim_ms)]
                phase = 'source_proof'
                source_proof_started=time.monotonic()
                recovered=await asyncio.to_thread(self.recoverable_outcome,job)
                source_proof_ms=int(max(0,time.monotonic()-source_proof_started)*1000)
                handler_ms=0
                if recovered is None:
                    phase = 'handler'
                    update = Update.de_json(job['decoded_payload'], self.application.bot)
                    context = CallbackContext.from_update(update, self.application)
                    context._rainbow_queue_resume = True
                    processing=asyncio.create_task(self.guarded_handler(job,update,context,is_callback))
                    try:
                        handler_started=time.monotonic()
                        done,_=await asyncio.wait((processing,heartbeat),timeout=getattr(self,'processing_timeout',300),return_when=asyncio.FIRST_COMPLETED)
                        if not done:
                            from execution_safety import resolve
                            value=self.executions.get((job['namespace'],job['chat_id'],job['message_id']))
                            if value:value['stopped'].set()
                            await asyncio.to_thread(resolve,self.queue.database,job,'ORDER_TIMEOUT')
                            continue
                        if heartbeat in done:
                            await heartbeat
                        await processing
                        handler_ms=int(max(0,time.monotonic()-handler_started)*1000)
                    finally:
                        if not processing.done():
                            processing.cancel()
                            await asyncio.gather(processing,return_exceptions=True)
                    if getattr(context, '_rainbow_processing_failed', False):
                        raise RuntimeError('ORDER_PROCESSING_FAILED')
                    if getattr(context,'_rainbow_technical_error',None):
                        from execution_safety import resolve
                        await asyncio.to_thread(resolve,self.queue.database,job,'ORDER_EXCEPTION')
                        continue
                final=recovered or ('COMPLETED' if is_callback else await asyncio.to_thread(self.outcome, job))
                if recovered=='COMPLETED':
                    from execution_safety import resolve
                    await asyncio.to_thread(resolve,self.queue.database,job,'ORDER_RECOVERY')
                    continue
                phase = 'finish'
                saved=await asyncio.to_thread(self.queue.finish,job,final,
                    'CALLBACK_PROCESSED' if is_callback else
                    {'COMPLETED':'ORDER_COMMITTED','NEED_CONFIRMATION':'AWAITING_CUSTOMER','REJECTED':'NOT_ACCEPTED'}[final])
                if not saved:
                    raise RuntimeError('ORDER_JOB_LEASE_LOST')
                logger.info('ORDER_QUEUE_PHASE phase=finish source=%s state=%s total_ms=%d',
                    source_tag,final,int(max(0,time.time()-job['received_at'])*1000))
                logger.info('ORDER_QUEUE_STAGE source=%s lane=%s source_proof_ms=%d handler_ms=%d worker_ms=%d recovered=%s',
                    source_tag,lane,source_proof_ms,handler_ms,
                    int(max(0,time.monotonic()-processing_started)*1000),bool(recovered))
                if is_callback:logger.info('CALLBACK_PHASE phase=finish state=PROCESSED')
                if not is_callback:
                    queue_metrics.extend([dict(mode=mode(),stage='WORKER_EXECUTION',elapsed_ms=max(0,time.monotonic()-processing_started)*1000),dict(mode=mode(),stage='QUEUE_TOTAL',elapsed_ms=max(0,time.time()-job['received_at'])*1000)])
                    await asyncio.to_thread(record_samples,queue_metrics)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error('ORDER_QUEUE_FAILURE phase=%s namespace=%s message_id=%s details=%s',phase,
                    job.get('namespace') if job else None,job.get('message_id') if job else None,safe_failure_details(error))
                if job:
                    if job['decoded_payload'].get('callback_query') and int(job.get('attempts') or 0)==0:
                        try:
                            await update.callback_query.answer('按钮处理暂时失败，请求已保存并将重试；请勿重复确认。')
                        except Exception as notification_error:
                            logger.warning('CALLBACK_PHASE phase=error_notice_failed kind=%s',type(notification_error).__name__)
                    recovered=None
                    try:
                        recovered=await asyncio.to_thread(self.recoverable_outcome,job)
                    except Exception as recovery_error:
                        logger.warning('ORDER_QUEUE_RECOVERY_UNAVAILABLE details=%s',safe_failure_details(recovery_error))
                    try:
                        if recovered=='NEED_CONFIRMATION':
                            await asyncio.to_thread(self.queue.finish,job,recovered,'AWAITING_CUSTOMER_RECOVERED')
                        else:
                            from execution_safety import resolve
                            reason=getattr(context,'_rainbow_technical_error',None) or 'ORDER_EXCEPTION'
                            await asyncio.to_thread(resolve,self.queue.database,job,reason)
                    except Exception as persistence_error:
                        # Failure reporting can hit the same writer lock as the
                        # original handler. Never let this kill the worker.
                        # Keep the original source/lease unchanged; expiry and
                        # source-proof recovery arbitrate the next attempt.
                        logger.error('ORDER_QUEUE_FAILURE_STATE_UNSAVED namespace=%s message_id=%s details=%s',
                            job['namespace'],job['message_id'],safe_failure_details(persistence_error))
                        if not hasattr(self,'uncertain_messages'):self.uncertain_messages={}
                        await self.uncertain_notice(job)
                await asyncio.sleep(1)
            finally:
                if heartbeat:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                from deployment_drain import release_job
                release_job(job)

    async def close(self):
        # Stop claiming new messages, but keep active workers and their lease
        # heartbeat alive until their existing bounded handler completes.
        # Cancelling here strands a valid job until the 120s lease expires.
        self.stopping.set()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.maintenance_task.cancel()
        await asyncio.gather(self.maintenance_task,return_exceptions=True)
