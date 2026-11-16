"""Transactional execution fencing and durable final receipts; no order replay."""
import contextvars
import hashlib
import html
import json
import threading
import time
import logging
from contextlib import contextmanager
from sqlalchemy import text, inspect

CURRENT = contextvars.ContextVar('rainbow_execution', default=None)
FAILURE_TEXT = ('❌ 订单处理失败\n\n本次订单因系统处理超时，未能成功录入。\n\n'
                '系统已结束本次处理，请重新发送完整订单。\n\n本次未创建订单，也未扣减库存。')


class ExecutionStopped(RuntimeError):
    pass

def failure_text(reason):
    explanations={
        'ADDRESS_TRUST_STORAGE_UNAVAILABLE':'地址校验凭据存储不可用，请联系管理员检查服务配置',
        'ADDRESS_SERVICE_FAILED':'地址校验服务异常，未能完成收货地址校验',
        'ADDRESS_ACCOUNT_BALANCE_INSUFFICIENT':'地址校验服务余额不足，请联系管理员',
        'ADDRESS_PROVIDER_RATE_LIMITED':'地址校验服务限流，请稍后重新发送',
        'ADDRESS_DEADLINE_EXCEEDED':'地址校验服务超时，请稍后重新发送',
        'ORDER_TIMEOUT':'系统处理超时',
        'ORDER_EXCEPTION':'系统处理异常',
        'NETWORK_ERROR':'网络连接超时或不稳定',
        'AI_DISABLED':'AI 服务暂不可用',
        'MODEL_ERROR':'AI 模型响应异常',
        'AUTH_ERROR':'服务鉴权失败，请联系管理员',
        'RATE_LIMIT':'操作过于频繁，请稍后重试',
        'HTTP_TIMEOUT':'网络请求超时',
        'HTTP_CONNECTION_ERROR':'无法连接到远程服务',
        'ACCOUNT_LOOKUP_UNAVAILABLE':'账户权限查询失败，请稍后重试',
        'ORDER_DRAFT_OPEN_UNCERTAIN':'订单草稿开启失败，请稍后重试',
        'ORDER_DRAFT_SAVE_FAILED':'订单草稿保存失败，请稍后重试',
    }
    detail=explanations.get(reason,f'系统处理异常 ({reason})' if reason else '系统处理异常')
    return FAILURE_TEXT.replace('系统处理超时',detail)


@contextmanager
def execution(job):
    state = dict(job=job, stopped=threading.Event())
    token = CURRENT.set(state)
    try:
        yield state
    finally:
        CURRENT.reset(token)


def ensure_running():
    value = CURRENT.get()
    if value and value['stopped'].is_set():
        raise ExecutionStopped('ORDER_EXECUTION_STOPPED')


def technical_failure(category):
    value=CURRENT.get()
    if value:
        # Keep the first failure authoritative; append rather than overwrite.
        with value.setdefault('failure_lock',threading.RLock()):
            value.setdefault('technical_error',category)
            allowed={'TIMEOUT','NETWORK_ERROR','AUTH_ERROR','CREDIT_ERROR','RATE_LIMIT','URL_ERROR','MODEL_ERROR','PROVIDER_ERROR','INVALID_RESPONSE','KEY_MISSING','AI_DISABLED','BUDGET_ERROR','HTTP_SERVER_ERROR','HTTP_CONNECTION_ERROR','HTTP_TIMEOUT','ADDRESS_BUDGET_EXHAUSTED','ADDRESS_MONTHLY_BUDGET_EXHAUSTED','ADDRESS_SERVICE_FAILED','ADDRESS_DEADLINE_EXCEEDED','ADDRESS_CASH_BUDGET_EXHAUSTED','ADDRESS_SERVICE_CIRCUIT_OPEN','ADDRESS_COST_GUARD_UNAVAILABLE','ADDRESS_USAGE_UNAVAILABLE'}
            allowed.update(('ADDRESS_ACCOUNT_BALANCE_INSUFFICIENT','ADDRESS_PROVIDER_RATE_LIMITED','ADDRESS_TRUST_STORAGE_UNAVAILABLE'))
            safe=category if isinstance(category,str) and category in allowed else 'UNKNOWN_TECHNICAL_ERROR'
            from network_safety import identity
            entry=dict(identity(),category=safe,at=time.time())
            failures=value.setdefault('technical_failures',[])
            if len(failures)<32:failures.append(entry)
            else:value['technical_failures_overflow']=value.get('technical_failures_overflow',0)+1
            logging.getLogger(__name__).warning('ORDER_FAILURE_CHAIN %s',json.dumps(dict(identity(),first_category=failures[0]['category'],failures=failures,overflow=value.get('technical_failures_overflow',0)),sort_keys=True))


def ensure_customer_validation():
    ensure_running()
    value=CURRENT.get()
    if value and value.get('technical_error'):
        raise ExecutionStopped('TECHNICAL_FAILURE_NOT_CUSTOMER_ERROR')


def credentials():
    ensure_running()
    value = CURRENT.get()
    if not value:
        return None
    return {k: value['job'][k] for k in ('namespace','chat_id','message_id','lease_token')}


def migrate(connection):
    for sql in (
        '''CREATE TABLE IF NOT EXISTS telegram_source_fences (
          namespace TEXT NOT NULL,chat_id TEXT NOT NULL,message_id INTEGER NOT NULL,
          reason TEXT NOT NULL,closed_at REAL NOT NULL,
          PRIMARY KEY(namespace,chat_id,message_id))''',
        '''CREATE TABLE IF NOT EXISTS telegram_execution_commits (
          namespace TEXT NOT NULL,chat_id TEXT NOT NULL,message_id INTEGER NOT NULL,
          order_id INTEGER NOT NULL REFERENCES orders(id),
          PRIMARY KEY(namespace,chat_id,message_id,order_id))''',
        '''CREATE TABLE IF NOT EXISTS telegram_execution_receipts (
          event_key TEXT PRIMARY KEY,chat_id TEXT NOT NULL,source_message_id INTEGER,
          body TEXT NOT NULL,edit_message_id INTEGER,status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,
          delivered_message_id INTEGER,error_code TEXT)''',
    ):
        connection.exec_driver_sql(sql)


def check_submit(session, execution_info, chat_id, message_id):
    ensure_running()
    from newbot_safety import validate_submit
    validate_submit(session,chat_id,message_id)
    if not inspect(session.connection()).has_table('telegram_source_fences'):
        if execution_info:
            raise ValueError('EXECUTION_FENCE_NOT_READY')
        return
    if chat_id is not None and message_id is not None:
        from telegram_source_identity import scope
        blocked = session.execute(text('''SELECT 1 FROM telegram_source_fences
          WHERE namespace IN (:namespace,'legacy:unknown') AND chat_id=:chat AND message_id=:message'''),
          dict(namespace=scope(chat_id),chat=str(chat_id),message=int(message_id))).scalar()
        if blocked:
            raise ValueError('ORDER_EXECUTION_CLOSED:本次订单处理已结束，请重新发送完整订单')
    if execution_info is not None:
        if not isinstance(execution_info,dict) or set(execution_info)!={'namespace','chat_id','message_id','lease_token'}:
            raise ValueError('INVALID_EXECUTION_CREDENTIALS')
        if chat_id is not None and str(execution_info['chat_id']) != str(chat_id):
            raise ValueError('EXECUTION_SOURCE_MISMATCH')
        active=session.execute(text('''SELECT 1 FROM telegram_order_jobs
          WHERE namespace=:namespace AND chat_id=:chat_id AND message_id=:message_id
          AND lease_token=:lease_token AND state='PROCESSING' AND lease_until>:now'''),
          dict(execution_info,now=time.time())).scalar()
        if not active:
            raise ValueError('ORDER_EXECUTION_CLOSED:任务租约已失效，禁止迟到提交')


def record_commits(session, execution_info, orders):
    if execution_info:
        for order in orders:
            session.execute(text('''INSERT OR IGNORE INTO telegram_execution_commits
              VALUES(:namespace,:chat_id,:message_id,:order_id)'''),dict(execution_info,order_id=order.id))


def _source(session, job):
    if job['message_id']>0:
        original=session.execute(text('''SELECT d.namespace,d.chat_id,d.message_id
          FROM telegram_message_events e JOIN telegram_order_drafts d
          ON d.token=e.draft_token AND d.namespace=e.namespace
          WHERE e.namespace=:namespace AND e.chat_id=:chat_id AND e.message_id=:message_id
          AND e.sender_id=:sender_id AND d.sender_id=:sender_id'''),job).first()
        if original:return tuple(original)
        return job['namespace'],job['chat_id'],job['message_id']
    token=job.get('draft_token')
    if token:
        row=session.execute(text('''SELECT namespace,chat_id,message_id FROM telegram_order_drafts
          WHERE token=:token AND chat_id=:chat AND sender_id=:sender'''),
          dict(token=token,chat=job['chat_id'],sender=job['sender_id'])).first()
        if row:
            return tuple(row)
    return None


def _orders(session, job, source):
    ids=set(session.execute(text('''SELECT order_id FROM telegram_execution_commits
      WHERE namespace=:namespace AND chat_id=:chat_id AND message_id=:message_id'''),job).scalars())
    if source:
        namespace,chat,message=source
        ids.update(session.execute(text('''SELECT id FROM orders WHERE source_chat_id=:chat
          AND (source_message_id=:message OR source_message_id IN (
            SELECT d.message_id FROM telegram_message_events e JOIN telegram_order_drafts d
            ON d.token=e.draft_token AND d.namespace=e.namespace
            WHERE e.namespace=:namespace AND e.chat_id=:chat AND e.message_id=:message))'''),
            dict(namespace=namespace,chat=chat,message=message)).scalars())
    return sorted(ids)


def _queue_receipt(session,job,source,order_ids,reason):
    if not source:
        return
    namespace,chat,message=source
    from six_unknown_receipt_hold import held_recovery, deny, ReceiptReplayHeld
    held=held_recovery('',chat,message)
    if held:
        try:deny(held,'recovery_generation')
        except ReceiptReplayHeld:return False
    draft=session.execute(text('''SELECT payload FROM telegram_order_drafts
      WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message'''),
      dict(namespace=namespace,chat=chat,message=message)).scalar()
    payload=json.loads(draft) if draft else {}
    if order_ids and payload.get('_group_receipt_closed') and not job.get('checking_message_id'):
        return
    edit_id=job.get('checking_message_id') or payload.get('_group_receipt_message_id')
    if not edit_id:
        prompts=payload.get('_reply_message_ids') or []
        edit_id=next((p for p in reversed(prompts) if type(p) is int and p>0),None)
    if order_ids:
        from models import Order
        # A direct Bot send can be accepted even if its reply timed out or
        # the following draft save failed. Never create a competing recovery
        # send after a durable direct SENT/SENDING/UNKNOWN attempt.
        try:
            from success_receipt_audit import records_for_order
            attempts=[attempt for oid in order_ids
                      for attempt in records_for_order(session.get(Order,oid).order_number)]
            if any(row.get('status') in ('SENT','SENDING','UNKNOWN') for row in attempts):
                logging.getLogger(__name__).warning('ORDER_RECOVERY_RECEIPT_HELD direct_delivery_observed=true')
                return
        except Exception:
            logging.getLogger(__name__).error('ORDER_RECOVERY_RECEIPT_HELD direct_audit_unavailable=true')
            return
        lines=['✅ 录单成功（原订单回执恢复）']
        for order_id in order_ids:
            order=session.get(Order,order_id)
            lines.extend(['订单编号：'+html.escape(order.business_order_number or order.order_number),
                          '收件人：'+html.escape(order.recipient_name),
                          '实际录入状态：'+html.escape(str(order.status))])
            for item in order.items:
                category=item.product.parent.name if item.product.parent else ''
                lines.append(html.escape(category+' · '+item.product.name)+' × '+str(item.quantity))
        body='\n'.join(lines)
    else:
        body=failure_text(reason)
    from order_receipt import receipt_chunks
    chunks=receipt_chunks(body.splitlines())
    identity=hashlib.sha256(f'{namespace}:{chat}:{message}'.encode()).hexdigest()
    queued=False
    for number,chunk in enumerate(chunks):
        inserted=session.execute(text('''INSERT OR IGNORE INTO telegram_execution_receipts
          (event_key,chat_id,source_message_id,body,edit_message_id,status,created_at)
          VALUES(:key,:chat,:message,:body,:edit,'pending',:now)'''),
          dict(key=f'final:{identity}:{number}',chat=chat,message=message,body=chunk,
               edit=edit_id if number==0 else None,now=time.time())).rowcount
        queued=queued or inserted>0
    return queued


def resolve(database,job,reason='ORDER_TIMEOUT',*,only_if_lease_expired=False):
    """Writer lock orders this decision against every in-flight order commit."""
    with database.SessionLocal() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row=session.execute(text('''SELECT * FROM telegram_order_jobs WHERE namespace=:namespace
          AND chat_id=:chat_id AND message_id=:message_id'''),job).mappings().first()
        if not row or row['state'] not in ('RECEIVED','PROCESSING','FAILED_RETRYABLE','REVIEW_REQUIRED'):
            return None
        if job.get('lease_token') and row['lease_token']!=job['lease_token']:
            return None
        if not job.get('lease_token') and row['state']!=job['state']:
            return None
        # Maintenance selected an expired snapshot outside this writer lock.
        # A heartbeat may have renewed it since; never fence that live job.
        if only_if_lease_expired and (row['state']!='PROCESSING'
                or row['lease_until'] is None or row['lease_until']>=time.time()):
            return None
        row=dict(row);source=_source(session,row);orders=_orders(session,row,source)
        if job.get('checking_message_id'):row['checking_message_id']=job['checking_message_id']
        if source and not orders:
            namespace,chat,message=source
            session.execute(text('''INSERT OR IGNORE INTO telegram_source_fences VALUES
              (:namespace,:chat,:message,:reason,:now)'''),dict(namespace=namespace,chat=chat,message=message,reason=reason,now=time.time()))
            session.execute(text('''UPDATE telegram_order_drafts SET status='timeout_failed',updated_at=:now
              WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message AND status='pending' '''),
              dict(namespace=namespace,chat=chat,message=message,now=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()))
        state='COMPLETED' if orders else 'TIMED_OUT'
        receipt_queued=_queue_receipt(session,row,source,orders,reason)
        session.execute(text('''UPDATE telegram_order_jobs SET state=:state,reason=:reason,
          lease_token=NULL,lease_until=NULL,finished_at=:now WHERE namespace=:namespace
          AND chat_id=:chat_id AND message_id=:message_id'''),dict(row,state=state,reason='ORDER_COMMITTED' if orders else reason,now=time.time()))
        session.commit()
        return dict(state=state,order_count=len(orders),receipt_queued=bool(receipt_queued))


async def deliver(database,bot,limit=4):
    """Durable one-attempt send; idempotent edits may be safely retried."""
    if limit<=0:return
    import asyncio
    from telegram.error import BadRequest
    from six_unknown_receipt_hold import held_recovery, deny, ReceiptReplayHeld
    keys=[]
    with database.SessionLocal() as session:
        # Held rows must not fill the batch and starve unrelated receipts.
        rows=session.execute(text("SELECT event_key,chat_id,source_message_id FROM telegram_execution_receipts WHERE status='pending' ORDER BY created_at")).mappings()
        try:
            for candidate in rows:
                held=held_recovery(candidate['event_key'],candidate['chat_id'],candidate['source_message_id'])
                if held:
                    try:deny(held,'recovery_selection')
                    except ReceiptReplayHeld:continue
                keys.append(candidate['event_key'])
                if len(keys)>=limit:break
        finally:rows.close()
    for key in keys:
        with database.SessionLocal() as session:
            # Inspect identity and claim in one existing SQLite writer turn;
            # held rows retain pending/status/attempts exactly as found.
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            snapshot=session.execute(text('SELECT * FROM telegram_execution_receipts WHERE event_key=:key AND status=\'pending\''),dict(key=key)).mappings().first()
            if not snapshot:continue
            from six_unknown_receipt_hold import held_recovery, deny, ReceiptReplayHeld
            held=held_recovery(key,snapshot['chat_id'],snapshot['source_message_id'])
            if held:
                try:deny(held,'recovery_dispatch')
                except ReceiptReplayHeld:continue
            changed=session.execute(text("UPDATE telegram_execution_receipts SET status='sending',attempts=attempts+1 WHERE event_key=:key AND status='pending'"),dict(key=key)).rowcount
            if not changed:continue
            row=dict(session.execute(text('SELECT * FROM telegram_execution_receipts WHERE event_key=:key'),dict(key=key)).mappings().one())
            session.commit()
        status='sent';error=None;message_id=None;send_started=False
        try:
            if row['edit_message_id']:
                try:
                    await asyncio.wait_for(bot.edit_message_text(chat_id=row['chat_id'],message_id=row['edit_message_id'],text=row['body'],parse_mode='HTML',reply_markup=None),timeout=15)
                    message_id=row['edit_message_id']
                except BadRequest as exc:
                    if 'message is not modified' in str(exc).lower():message_id=row['edit_message_id']
                    elif any(s in str(exc).lower() for s in ('message to edit not found',"message can't be edited")):
                        send_started=True
                        with database.SessionLocal() as session:
                            session.execute(text('UPDATE telegram_execution_receipts SET edit_message_id=NULL WHERE event_key=:key'),dict(key=key));session.commit()
                        sent=await asyncio.wait_for(bot.send_message(chat_id=row['chat_id'],text=row['body'],parse_mode='HTML',reply_to_message_id=row['source_message_id']),timeout=15)
                        message_id=sent.message_id
                    else:raise
            else:
                send_started=True
                sent=await asyncio.wait_for(bot.send_message(chat_id=row['chat_id'],text=row['body'],parse_mode='HTML',reply_to_message_id=row['source_message_id']),timeout=15)
                message_id=sent.message_id
        except Exception as exc:
            # A send timeout is ambiguous. Never retry a potentially accepted send.
            status='pending' if row['edit_message_id'] and not send_started and row['attempts']<3 else 'unknown'
            error=type(exc).__name__
        with database.SessionLocal() as session:
            session.execute(text('''UPDATE telegram_execution_receipts SET status=:status,
              delivered_message_id=:message,error_code=:error WHERE event_key=:key'''),
              dict(status=status,message=message_id,error=error,key=key));session.commit()


def recover_dispatches(database):
    """Observe interrupted sends without rewriting/rearming their original rows.

    Sending is delivery-unknown after interruption, NOT permission to retry an
    edit or send. Future pending receipts continue through deliver unchanged.
    """
    with database.SessionLocal() as session:
        frozen = session.execute(text(
            "SELECT COUNT(*) FROM telegram_execution_receipts WHERE status='sending'"
        )).scalar_one()
    return dict(interrupted_sending=frozen, original_rows_changed=0,
                automatically_rearmed=0)
