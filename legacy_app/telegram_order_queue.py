"""Durable message receipt and leased processing, independent of business writes.

Replay payloads are encrypted operational data, never diagnostic log text.
No cleanup operation or process startup deletes receipts.
"""
import base64
import hashlib
import json
import time
import threading
import weakref
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import text
from telegram_source_identity import parameters, scope

TERMINAL = frozenset({'COMPLETED', 'NEED_CONFIRMATION', 'REJECTED', 'CANCELLED_TEST','TIMED_OUT'})
REVIEW_STATE = 'REVIEW_REQUIRED'
MAX_AUTOMATIC_ATTEMPTS = 5


def migrate(connection):
    from execution_safety import migrate as migrate_execution
    migrate_execution(connection)
    from queue_recovery_admin import migrate as migrate_recovery
    migrate_recovery(connection)
    connection.execute(text('''CREATE TABLE IF NOT EXISTS telegram_order_jobs (
        namespace TEXT NOT NULL DEFAULT 'legacy:unknown',
        chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
        sender_id TEXT NOT NULL, payload TEXT NOT NULL,
        fingerprint TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'RECEIVED',
        received_at REAL NOT NULL, source_date REAL, started_at REAL, finished_at REAL,
        lease_token TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
        reason TEXT, PRIMARY KEY(namespace,chat_id,message_id))'''))
    connection.execute(text('CREATE INDEX IF NOT EXISTS ix_telegram_jobs_ready ON telegram_order_jobs(state,lease_until,received_at)'))
    connection.execute(text('CREATE INDEX IF NOT EXISTS ix_telegram_jobs_chat_started ON telegram_order_jobs(namespace,chat_id,started_at)'))
    connection.execute(text('CREATE INDEX IF NOT EXISTS ix_telegram_jobs_actor_state ON telegram_order_jobs(namespace,chat_id,sender_id,state)'))
    from sqlalchemy import inspect
    if 'source_date' not in {c['name'] for c in inspect(connection).get_columns('telegram_order_jobs')}:
        connection.execute(text('ALTER TABLE telegram_order_jobs ADD COLUMN source_date REAL'))
    if 'draft_token' not in {c['name'] for c in inspect(connection).get_columns('telegram_order_jobs')}:
        connection.execute(text('ALTER TABLE telegram_order_jobs ADD COLUMN draft_token TEXT'))
    connection.execute(text('CREATE INDEX IF NOT EXISTS ix_telegram_jobs_draft_token ON telegram_order_jobs(draft_token,received_at)'))


class Queue:
    def __init__(self, database, secret):
        if not secret or secret == 'SESSION_SECRET_KEY' or len(secret) < 32:
            raise ValueError('可靠订单队列需要现有有效会话密钥')
        self.database = database
        self._independent_cache = {}
        self._classification_guard = threading.RLock()
        self._classification_locks = weakref.WeakValueDictionary()
        self.cipher = Fernet(base64.urlsafe_b64encode(
            hashlib.sha256(('rainbow-order-replay-v1:' + secret).encode()).digest()))

    def recover_after_restart(self, guard, *, after, protected=()):
        """Fence only unowned new work while the Bot single-instance lock is held.

        No order/stock/ledger/receipt writes. Old incidents, drafts, callbacks,
        closed sources and any attempted receipt are never replay candidates.
        """
        import fcntl,math
        from datetime import datetime,timezone
        if guard is None or not isinstance(after,(int,float)) or not math.isfinite(after) or after<=0:
            raise ValueError('RESTART_RECOVERY_GUARD_REQUIRED')
        fcntl.flock(guard.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        from telegram_source_identity import bot_id
        from p0_recovery_protection import identity
        with self.database.engine.connect() as c:
            candidates=[dict(r) for r in c.execute(text('''SELECT * FROM telegram_order_jobs
                WHERE state='PROCESSING' AND received_at>:after AND message_id>0
                AND namespace IN ('shared',:observer) ORDER BY received_at LIMIT 64'''),
                {'after':after,'observer':'bot:'+bot_id()}).mappings()]
        candidates=[r for r in candidates if identity(r) not in protected
                    and r.get('lease_token') and not r.get('draft_token')]
        # Classification/decryption stays outside the business writer lock.
        independent={identity(r):self._independent_complete(r) for r in candidates}
        recovered=completed=0
        with self.database.SessionLocal() as s:
            s.connection().exec_driver_sql('BEGIN IMMEDIATE')
            for snapshot in candidates:
                args=dict(namespace=snapshot['namespace'],chat=snapshot['chat_id'],message=snapshot['message_id'])
                row=s.execute(text('''SELECT * FROM telegram_order_jobs WHERE namespace=:namespace
                    AND chat_id=:chat AND message_id=:message AND state='PROCESSING' '''),args).mappings().first()
                if (not row or row['lease_token']!=snapshot['lease_token']
                        or row['fingerprint']!=snapshot['fingerprint']
                        or row['payload']!=snapshot['payload']
                        or row['draft_token']!=snapshot['draft_token']):continue
                commits=set(s.execute(text('''SELECT order_id FROM telegram_execution_commits
                    WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message'''),args).scalars())
                orders=s.execute(text('''SELECT o.id,u.telegram_id FROM orders o JOIN users u ON u.id=o.user_id
                    WHERE o.source_chat_id=:chat AND o.source_message_id=:message'''),args).all()
                order_ids={r[0] for r in orders}
                if commits and commits==order_ids and all(str(r[1])==str(row['sender_id']) for r in orders):
                    state,reason='COMPLETED','RESTART_ORDER_COMMIT_OBSERVED'
                else:
                    if commits or order_ids or row['attempts']>=MAX_AUTOMATIC_ATTEMPTS or not independent[identity(snapshot)]:continue
                    fence=s.execute(text('''SELECT 1 FROM telegram_source_fences WHERE namespace IN (:namespace,'legacy:unknown')
                        AND chat_id=:chat AND message_id=:message'''),args).first()
                    receipt=s.execute(text('''SELECT 1 FROM telegram_execution_receipts
                        WHERE chat_id=:chat AND source_message_id=:message LIMIT 1'''),args).first()
                    draft=s.execute(text('''SELECT 1 FROM telegram_order_drafts WHERE chat_id=:chat
                        AND message_id=:message LIMIT 1'''),args).first()
                    if fence or receipt or draft:continue
                    state,reason='RECEIVED','RESTART_UNCOMMITTED_FENCED'
                now=time.time()
                changed=s.execute(text('''UPDATE telegram_order_jobs SET state=:state,reason=:reason,
                    lease_token=NULL,lease_until=NULL,finished_at=:finished
                    WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message
                    AND state='PROCESSING' AND lease_token=:old'''),
                    dict(args,state=state,reason=reason,finished=now if state=='COMPLETED' else None,old=row['lease_token'])).rowcount
                if not changed:continue
                operation=hashlib.sha256(('restart:'+identity(snapshot)+':'+row['lease_token']).encode()).hexdigest()
                s.execute(text('''INSERT INTO telegram_queue_recovery_audit
                    (operation_key,namespace,chat_id,message_id,old_state,new_state,operator,reason,
                     source_fingerprint,created_at,result)
                    VALUES(:operation,:namespace,:chat,:message,'PROCESSING',:state,'SYSTEM_RESTART',:reason,
                     :fingerprint,:created,:result)'''),dict(args,operation=operation,state=state,reason=reason,
                    fingerprint=row['fingerprint'],created=datetime.now(timezone.utc).isoformat(),
                    result=json.dumps({'business_writes':False,'receipts_replayed':False,'new_state':state})))
                completed+=int(state=='COMPLETED');recovered+=int(state=='RECEIVED')
            s.commit()
        return dict(requeued=recovered,committed_observed=completed,business_writes=0,receipts_replayed=0)

    def receive(self, chat_id, message_id, sender_id, payload, now=None):
        from newbot_safety import payload_allowed
        if not payload_allowed(chat_id,payload):return False
        # Negative operational keys are callback update IDs, not Telegram
        # message IDs. Update IDs are bot-specific even in a supergroup.
        namespace = scope(1 if int(message_id)<0 else chat_id)
        received = time.time() if now is None else now
        message = payload.get('message')
        date = message.get('date') if isinstance(message,dict) and int(message_id)>0 else None
        source_date = float(date) if type(date) in (int,float) and date>0 else None
        from telegram_source_identity import account_cutover
        boundary=account_cutover()
        historical=False
        if boundary is not None:
            origin=payload.get('message') or payload.get('callback_query',{}).get('message') or {}
            stamp=origin.get('date')
            historical=type(stamp) not in (int,float) or stamp<boundary.timestamp()
        # The replacement bot must not turn old account-specific IDs into new
        # business candidates. This gate is independent of the deduplication key.
        with self.database.engine.connect() as connection:
            from sqlalchemy import inspect
            if inspect(connection).has_table('telegram_message_observers') and namespace != 'shared':
                activated = connection.execute(text('SELECT activated_at FROM telegram_message_observers WHERE bot_id=:bot'),
                    dict(bot=namespace.partition(':')[2])).scalar()
                message = payload.get('message') or payload.get('callback_query', {}).get('message') or {}
                date = message.get('date')
                if activated and boundary is None:
                    from datetime import datetime
                    if not isinstance(date, (int,float)) or date < datetime.fromisoformat(activated).timestamp():
                        return False
        raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        if len(raw) > 262144:
            raise ValueError('订单消息超出安全长度')
        encrypted = self.cipher.encrypt(raw).decode()
        callback_data=str((payload.get('callback_query') or {}).get('data') or '')
        draft_token=None
        if payload.get('callback_query'):
            import re
            match=re.fullmatch(r'(?:order_(?:confirm|cancel)|ai_flavor:[^:]+|manual_review:[^:]+|draft_cancel|name_generate|address_confirm):([A-Za-z0-9_-]{8,128})',callback_data)
            if match: draft_token=match.group(1)
        with self.database.engine.begin() as connection:
            inserted=bool(connection.execute(text('''INSERT INTO telegram_order_jobs
                (namespace,chat_id,message_id,sender_id,payload,fingerprint,received_at,source_date,draft_token,state,reason)
                VALUES(:namespace,:chat,:message,:sender,:payload,:fingerprint,:now,:source_date,:draft_token,:state,:reason)
                ON CONFLICT(namespace,chat_id,message_id) DO NOTHING'''), {
                    'namespace': namespace,
                    'chat': str(chat_id), 'message': int(message_id),
                    'sender': str(sender_id), 'payload': encrypted,
                    'fingerprint': hashlib.sha256(raw).hexdigest(),
                    'source_date': source_date, 'draft_token':draft_token, 'now': received,
                    'state':REVIEW_STATE if historical else 'RECEIVED',
                    'reason':'BOT_ACCOUNT_CUTOVER_HISTORICAL_UPDATE' if historical else None}).rowcount)
        from order_trace import source_event
        source_event('QUEUE_DURABLE',namespace,str(chat_id),int(message_id),telegram_at=source_date,
                     received_at=received,durable_at=time.time(),inserted=inserted)
        return inserted

    def _independent_complete(self, row, classify=True):
        fingerprint=row['fingerprint']
        with self._classification_guard:
            if fingerprint in self._independent_cache:
                return self._independent_cache[fingerprint]
            if not classify:
                return False
            # Only identical immutable payloads share work. Unrelated orders
            # retain existing bounded concurrency; no global parsing lock.
            lock=self._classification_locks.get(fingerprint)
            if lock is None:
                lock=threading.Lock()
                self._classification_locks[fingerprint]=lock
        with lock:
            with self._classification_guard:
                if fingerprint in self._independent_cache:
                    return self._independent_cache[fingerprint]
            return self._classify_independent(row)

    def _classify_independent(self, row):
        fingerprint=row['fingerprint']
        eligible=False
        try:
            payload=json.loads(self.cipher.decrypt(row['payload'].encode()))
            message=payload.get('message') or {}
            # Only a complete standalone original may bypass a predecessor.
            # No callbacks, quoted supplements or partial contact messages.
            if int(row['message_id'])>0 and not payload.get('callback_query') and not message.get('reply_to_message'):
                from contact_fields import parse_contact,diagnose_contact,merchandise_text
                body=message.get('text') or ''
                fields=parse_contact(body)
                diagnostics=diagnose_contact(fields)
                goods=merchandise_text(body,fields)
                import re
                eligible=bool(not diagnostics['missing'] and not diagnostics['invalid']
                    and goods.strip() and re.search(r'[^\d\s]\s*[xX×*：:]?\s*\d+',goods))
                if not eligible and not diagnostics['missing'] and not diagnostics['invalid'] and goods.strip():
                    # Default-one complete originals are independent too.
                    # This only isolates scheduling; catalog/quantity/auth and
                    # transactional source fences still run in the handler.
                    from order_parser import OrderParser
                    parsed=OrderParser.extract_order_info(body)
                    eligible=bool(isinstance(parsed,dict) and parsed.get('items')
                        and all(type(qty) is int and qty>0 for _,qty in parsed['items']))
        except Exception:
            # Unknown payload/interpretation keeps the original ordering gate.
            eligible=False
        with self._classification_guard:
            if len(self._independent_cache)>=512:
                self._independent_cache.pop(next(iter(self._independent_cache)))
            self._independent_cache[fingerprint]=eligible
        return eligible

    def _candidate_rows(self, connection, now, lane=None):
        from telegram_source_identity import account_cutover
        boundary=account_cutover()
        cutoff=boundary.timestamp() if boundary else None
        chat_limit=max(0,int(getattr(self,'message_chat_limit',0)))
        protection=getattr(self,'p0_protection',None)
        held_earlier=held_current=''
        if protection is not None:
            # Connection-local predicate only: no schema/data writes or stored
            # cache. Held incidents must not remain FIFO predecessors, nor fill
            # the bounded candidate window before unrelated menus/new work.
            fields=('namespace','chat_id','message_id','sender_id','state',
                    'received_at','source_date','started_at','finished_at',
                    'lease_until','lease_token','attempts','fingerprint',
                    'draft_token','payload')
            actual=connection.connection() if callable(getattr(connection,'connection',None)) else connection
            driver=actual.connection.driver_connection
            driver.create_function('newbot_p0_hold',len(fields),
                lambda *values:int(protection.blocks(dict(zip(fields,values)),self.cipher)))
            held_earlier='AND newbot_p0_hold('+','.join('earlier.'+f for f in fields)+')=0'
            held_current='AND newbot_p0_hold('+','.join('j.'+f for f in fields)+')=0'
        rows=connection.execute(text('''SELECT j.*, EXISTS (
                    SELECT 1 FROM telegram_order_jobs earlier
                    WHERE earlier.namespace IN ('shared',:observer)
                    AND (:cutoff IS NULL OR earlier.received_at>=:cutoff)
                    AND earlier.reason IS NOT 'BOT_ACCOUNT_CUTOVER_HISTORICAL_UPDATE'
                    AND earlier.chat_id=j.chat_id AND earlier.sender_id=j.sender_id
                    AND earlier.state IN ('RECEIVED','FAILED_RETRYABLE','PROCESSING','REVIEW_REQUIRED')
                    {held_earlier}
                    AND (COALESCE(earlier.source_date,earlier.received_at),earlier.message_id)
                       < (COALESCE(j.source_date,j.received_at),j.message_id)) AS predecessor_pending,
                EXISTS (SELECT 1 FROM telegram_order_jobs busy
                    WHERE busy.namespace IN ('shared',:observer)
                    AND busy.chat_id=j.chat_id AND busy.sender_id=j.sender_id
                    AND (:cutoff IS NULL OR busy.received_at>=:cutoff)
                    AND busy.state='PROCESSING' AND busy.lease_until>:now) AS actor_busy
                FROM telegram_order_jobs j INDEXED BY ix_telegram_jobs_ready
                WHERE j.state IN ('RECEIVED','FAILED_RETRYABLE','PROCESSING')
                AND (j.state IN ('RECEIVED','FAILED_RETRYABLE')
                    OR (:allow_expired=1 AND j.state='PROCESSING' AND j.lease_until<=:now))
                AND COALESCE(j.lease_until,0)<=:now
                {held_current}
                AND (:chat_limit=0 OR j.message_id<0 OR (SELECT count(*) FROM telegram_order_jobs chat_busy
                    WHERE chat_busy.namespace IN ('shared',:observer)
                    AND chat_busy.chat_id=j.chat_id AND chat_busy.message_id>=0
                    AND (:cutoff IS NULL OR chat_busy.received_at>=:cutoff)
                    AND chat_busy.state='PROCESSING' AND chat_busy.lease_until>:now)<:chat_limit)
                AND j.namespace IN ('shared',:observer)
                AND (:cutoff IS NULL OR (j.received_at>=:cutoff AND j.reason IS NOT 'BOT_ACCOUNT_CUTOVER_HISTORICAL_UPDATE'))
                AND (:lane IS NULL OR (:lane='callback' AND j.message_id<0)
                    OR (:lane='message' AND j.message_id>=0))
                AND (j.source_date IS NULL OR j.received_at<=:now-0.15)
                ORDER BY CASE WHEN j.state='RECEIVED' THEN 0 ELSE 1 END,
                    COALESCE((SELECT MAX(peer.started_at) FROM telegram_order_jobs peer
                        WHERE peer.namespace IN ('shared',:observer) AND peer.chat_id=j.chat_id),0),
                    COALESCE(source_date,received_at),message_id LIMIT 256'''.format(held_earlier=held_earlier,held_current=held_current)),
                {'now':now,'observer':scope(1),'lane':lane,'cutoff':cutoff,'chat_limit':chat_limit,
                 'allow_expired':int(not getattr(self,'expiry_requires_fence',False))}).mappings().all()
        return rows

    def _ready_candidate(self, connection, now, classify=True, lane=None):
        return self._choose_candidate(self._candidate_rows(connection,now,lane),classify)

    def _choose_candidate(self, rows, classify=True):
        for row in rows:
            protection=getattr(self,'p0_protection',None)
            if protection is not None and protection.blocks(row,self.cipher):
                continue
            # Supplements, quoted replies and callbacks retain actor FIFO.
            # Only a complete standalone order may bypass serialization.
            if ((not row['predecessor_pending'] and not row['actor_busy'])
                    or self._independent_complete(row,classify)):
                return row
        return None

    def claim(self, now=None, lease_seconds=120, lane=None):
        from newbot_safety import require_ready
        try:require_ready()
        except ValueError:return None
        from deployment_drain import begin_activity,end_activity
        active=begin_activity()
        if active is None:return None
        try:
            result=self._claim_active(now,lease_seconds,lane)
            if result is not None:
                result['_deployment_activity']=active;active=None
            return result
        finally:end_activity(active)

    def _claim_active(self, now=None, lease_seconds=120, lane=None):
        if lane not in (None, 'message', 'callback'):
            raise ValueError('无效队列通道')
        supplied_clock = now is not None
        now = time.time() if now is None else now
        claim_started_at = now
        timer=time.perf_counter
        phase_start=timer()
        # Idle/blocked workers must not compete for the business writer lock.
        # This is only a read-only hint; recheck under the original write lock
        # before leasing, so concurrent consumers cannot double-claim.
        with self.database.engine.connect() as connection:
            connected_at=timer()
            rows=self._candidate_rows(connection,now,lane)
            queried_at=timer()
        # Detached rows only: parsing or waiting for an identical in-flight
        # classification must never occupy a database pool connection.
        if self._choose_candidate(rows) is None:
            return None
        classified_at=timer()
        with self.database.SessionLocal() as session:
            session.connection()
            writer_connected_at=timer()
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            writer_acquired_at=timer()
            # Parsing and decryption happen only in the read-only probe.
            # Recheck gates atomically without holding the writer during parsing.
            checked_at=now if supplied_clock else time.time()
            row=self._ready_candidate(session,checked_at,classify=False,lane=lane)
            if row is None:
                return None
            if row['attempts'] >= MAX_AUTOMATIC_ATTEMPTS:
                session.execute(text('''UPDATE telegram_order_jobs SET state='REVIEW_REQUIRED',
                    reason='RETRY_BUDGET_EXHAUSTED',finished_at=:now,
                    lease_token=NULL,lease_until=NULL
                    WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message'''),
                    dict(now=now,namespace=row['namespace'],chat=row['chat_id'],message=row['message_id']))
                session.commit()
                return None
            result = dict(row)
            token = uuid.uuid4().hex
            rechecked_at=timer()
            # Probe/SQLite waits are queueing, not execution. Start the lease
            # when ownership is written, never from a stale pre-probe clock.
            claimed_at=now if supplied_clock else time.time()
            session.execute(text('''UPDATE telegram_order_jobs SET state='PROCESSING',
                lease_token=:token,lease_until=:until,started_at=:now,attempts=attempts+1
                WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message'''), {
                    'namespace': row['namespace'],
                    'token': token, 'until': claimed_at + lease_seconds, 'now': claimed_at,
                    'chat': row['chat_id'], 'message': row['message_id']})
            session.commit()
        result['lease_token'] = token
        result['started_at'] = claimed_at
        result['lease_until'] = claimed_at + lease_seconds
        from order_trace import source_event
        source_event('WORKER_CLAIMED',result['namespace'],result['chat_id'],result['message_id'],
                     received_at=result['received_at'],claimed_at=claimed_at,
                     claim_started_at=claim_started_at,lease_committed_at=time.time(),
                     read_pool_ms=(connected_at-phase_start)*1000,probe_sql_ms=(queried_at-connected_at)*1000,
                     classification_ms=(classified_at-queried_at)*1000,
                     writer_pool_ms=(writer_connected_at-classified_at)*1000,
                     writer_lock_ms=(writer_acquired_at-writer_connected_at)*1000,
                     atomic_recheck_ms=(rechecked_at-writer_acquired_at)*1000,
                     lease_write_commit_ms=(timer()-rechecked_at)*1000)
        result['decoded_payload'] = json.loads(self.cipher.decrypt(result['payload'].encode()))
        return result

    def renew(self, job, now=None, lease_seconds=120):
        with self.database.engine.begin() as connection:
            return bool(connection.execute(text('''UPDATE telegram_order_jobs
                SET lease_until=:until WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message
                AND lease_token=:token AND state='PROCESSING' '''), {
                    'namespace': job['namespace'],
                    'until': (time.time() if now is None else now) + lease_seconds,
                    'chat': job['chat_id'], 'message': job['message_id'],
                    'token': job['lease_token']}).rowcount)

    def reconcile_confirmations(self, now=None):
        from deployment_drain import begin_activity,end_activity
        activity=begin_activity()
        if activity is None:return
        try:return self._reconcile_confirmations_active(now)
        finally:end_activity(activity)

    def _reconcile_confirmations_active(self, now=None):
        """Callbacks resolve the original receipt through existing business facts."""
        now=time.time() if now is None else now
        # Idle reconciliation should not hold the writer while traversing all
        # pending drafts. Snapshot facts first, then conditionally update only
        # resolved rows in a brief transaction; business order facts win.
        resolved=[]
        with self.database.engine.connect() as c:
            from telegram_source_identity import account_cutover,source_order_exists
            boundary=account_cutover()
            rows=c.execute(text("SELECT namespace,chat_id,message_id FROM telegram_order_jobs WHERE state='NEED_CONFIRMATION' AND namespace IN ('shared',:observer) AND (:cutoff IS NULL OR received_at>=:cutoff)"),dict(observer=scope(1),cutoff=boundary.timestamp() if boundary else None)).mappings().all()
            for row in rows:
                params={'namespace':row['namespace'],'chat':row['chat_id'],'message':row['message_id']}
                order=source_order_exists(c,row['namespace'],row['chat_id'],row['message_id'])
                completed=c.execute(text('''SELECT d.status FROM telegram_message_events e
                    JOIN telegram_order_drafts d ON d.token=e.draft_token
                    WHERE e.namespace=:namespace AND d.namespace=e.namespace AND e.chat_id=:chat AND e.message_id=:message'''),params).scalar()
                if order or completed in ('completed','flavor_expired'):
                    resolved.append(dict(params,state='COMPLETED' if order else 'REJECTED',
                        reason='ORDER_COMMITTED' if order else 'FLAVOR_EXPIRED' if completed=='flavor_expired' else 'DRAFT_CLOSED',now=now))
        if resolved:
            with self.database.engine.begin() as c:
                for params in resolved[:50]:
                    # Recheck order truth after acquiring the writer. A newer
                    # commit cannot be overwritten by a stale expiry snapshot.
                    order=source_order_exists(c,params['namespace'],params['chat'],params['message'])
                    if order:
                        params.update(state='COMPLETED',reason='ORDER_COMMITTED')
                    c.execute(text('''UPDATE telegram_order_jobs SET state=:state,reason=:reason,
                        finished_at=:now WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message
                        AND state='NEED_CONFIRMATION' '''),params)

    def finish(self, job, state, reason=None, now=None):
        if state not in TERMINAL | {'FAILED_RETRYABLE', REVIEW_STATE}:
            raise ValueError('无效任务状态')
        if reason and (len(reason) > 80 or not all(c.isupper() or c.isdigit() or c == '_' for c in reason)):
            raise ValueError('诊断原因必须是脱敏代码')
        now = time.time() if now is None else now
        with self.database.engine.begin() as connection:
            attempts=connection.execute(text('''SELECT attempts FROM telegram_order_jobs
                WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message
                AND lease_token=:token AND state='PROCESSING' '''),dict(namespace=job['namespace'],
                chat=job['chat_id'],message=job['message_id'],token=job['lease_token'])).scalar()
            if attempts is None:
                return False
            if state=='FAILED_RETRYABLE' and attempts>=MAX_AUTOMATIC_ATTEMPTS:
                state,reason=REVIEW_STATE,'RETRY_BUDGET_EXHAUSTED'
            delay=min(300,30*2**min(max(0,attempts-1),4))
            return bool(connection.execute(text('''UPDATE telegram_order_jobs
                SET state=:state,reason=:reason,finished_at=:finished,
                    lease_until=:until,lease_token=NULL,
                    payload=payload
                WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message AND lease_token=:token
                    AND state='PROCESSING' '''), {
                        'namespace': job['namespace'], 'state': state, 'reason': reason,
                        'finished': now if state in TERMINAL | {REVIEW_STATE} else None,
                        'until': now + delay if state == 'FAILED_RETRYABLE' else None,
                        'chat': job['chat_id'], 'message': job['message_id'],
                        'token': job['lease_token']}).rowcount)
