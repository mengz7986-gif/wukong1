"""Dedicated group identity, durable draft/message facts and account-scoped views.

No import-time migration. Existing orders/payments remain the only ledger.
"""
import json
import secrets
import re
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import text, or_
from models import User, Order, DropshipAuthRequest
from telegram_source_identity import parameters, scope

DDL=(
    '''CREATE TABLE IF NOT EXISTS telegram_business_operations (
       operation_key TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
       kind TEXT NOT NULL, request_hash TEXT NOT NULL, result TEXT,
       created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS telegram_account_groups (
       chat_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
       title TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
       bot_status TEXT NOT NULL DEFAULT 'unknown', last_message_at TEXT, last_notice_at TEXT,
       bound_by TEXT NOT NULL, bound_at TEXT NOT NULL)''',
    'CREATE INDEX IF NOT EXISTS ix_telegram_groups_account ON telegram_account_groups(user_id,active)',
    '''CREATE TABLE IF NOT EXISTS telegram_order_drafts (
       namespace TEXT NOT NULL DEFAULT 'legacy:unknown',
       token TEXT PRIMARY KEY, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
       sender_id TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
       status TEXT NOT NULL DEFAULT 'pending', waiting_field TEXT,
       payload TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
       updated_at TEXT NOT NULL, UNIQUE(namespace,chat_id,message_id))''',
    '''CREATE TABLE IF NOT EXISTS telegram_message_events (
       namespace TEXT NOT NULL DEFAULT 'legacy:unknown',
       chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
       sender_id TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
       draft_token TEXT REFERENCES telegram_order_drafts(token), status TEXT NOT NULL,
       created_at TEXT NOT NULL, PRIMARY KEY(namespace,chat_id,message_id))''',
    '''CREATE TABLE IF NOT EXISTS telegram_notification_events (
       event_key TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
       chat_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
       status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
       created_at TEXT NOT NULL, sent_at TEXT, last_error TEXT)''',
)


def migrate(connection):
    for sql in DDL:connection.exec_driver_sql(sql)


def rollback(connection):
    # Only an empty candidate migration can be discarded. Real event facts
    # require a verified backup/recovery plan, never automatic DROP.
    tables=('telegram_notification_events','telegram_message_events','telegram_order_drafts','telegram_account_groups','telegram_business_operations')
    for table in tables:
        if connection.exec_driver_sql('SELECT COUNT(*) FROM '+table).scalar():
            raise ValueError('存在群绑定/草稿/通知事实，禁止破坏性Schema回滚')
    for table in tables:connection.exec_driver_sql('DROP TABLE '+table)


def eligibility_filter():
    """One SQL policy for current authorization and formal user visibility."""
    from sqlalchemy import select
    latest_status=(select(DropshipAuthRequest.status)
                   .where(DropshipAuthRequest.user_id==User.id)
                   .order_by(DropshipAuthRequest.created_at.desc(),DropshipAuthRequest.id.desc())
                   .limit(1).correlate(User).scalar_subquery())
    return (User.is_active.is_(True)) & (latest_status=='approved')

def eligible(session,user):
    if user is None:return False
    # Read columns, not cached ORM attributes; final order transactions call
    # this under BEGIN IMMEDIATE, so revocation cannot race their writes.
    return session.query(User.id).filter(User.id==user.id,eligibility_filter()).first() is not None


def begin_operation(session,key,user_id,kind,request):
    """Operational idempotency only; amounts stay in existing ledger tables."""
    import hashlib
    if not isinstance(key,str) or not 16<=len(key)<=128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-:' for c in key):
        raise ValueError('请刷新页面后重新提交，操作凭据无效')
    fingerprint=hashlib.sha256(json.dumps(request,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    row=session.execute(text('SELECT * FROM telegram_business_operations WHERE operation_key=:key'),dict(key=key)).mappings().first()
    if row:
        if row['user_id']!=user_id or row['kind']!=kind or row['request_hash']!=fingerprint:
            raise ValueError('重复操作凭据与本次内容冲突，禁止执行')
        if row['result'] is None:raise ValueError('操作处理中，请勿重复提交')
        return json.loads(row['result'])
    session.execute(text('INSERT INTO telegram_business_operations(operation_key,user_id,kind,request_hash,created_at) VALUES(:key,:user,:kind,:hash,:now)'),
        dict(key=key,user=user_id,kind=kind,hash=fingerprint,now=datetime.now(timezone.utc).isoformat()))
    return None


def finish_operation(session,key,result):
    session.execute(text('UPDATE telegram_business_operations SET result=:result WHERE operation_key=:key'),dict(key=key,result=json.dumps(result,ensure_ascii=False)))


def bound_account(session,chat_id):
    row=session.execute(text('SELECT * FROM telegram_account_groups WHERE chat_id=:chat AND active=1'),
                        dict(chat=str(chat_id))).mappings().first()
    return (session.get(User,row['user_id']),dict(row)) if row else (None,None)


def bind(session,user_id,chat_id,title,actor,replace=False):
    user=session.get(User,int(user_id))
    if user is None:raise ValueError('客户不存在')
    chat_id=str(int(chat_id))
    if int(chat_id)>=0:raise ValueError('接单群必须是Telegram群chat_id，不能使用用户ID')
    old=session.execute(text('SELECT * FROM telegram_account_groups WHERE chat_id=:chat'),dict(chat=chat_id)).mappings().first()
    if old and old['active'] and old['user_id']!=user.id:
        raise ValueError('该接单群已绑定其他客户，不能覆盖')
    if not replace and session.execute(text('SELECT 1 FROM telegram_account_groups WHERE user_id=:user AND active=1 AND chat_id<>:chat'),dict(user=user.id,chat=chat_id)).first():
        raise ValueError('客户已有接单群，请使用更换操作')
    if replace:session.execute(text('UPDATE telegram_account_groups SET active=0 WHERE user_id=:user'),dict(user=user.id))
    now=datetime.now(timezone.utc).isoformat()
    session.execute(text('''INSERT INTO telegram_account_groups(chat_id,user_id,title,bound_by,bound_at)
        VALUES(:chat,:user,:title,:actor,:now) ON CONFLICT(chat_id) DO UPDATE SET
        user_id=excluded.user_id,title=excluded.title,active=1,bound_by=excluded.bound_by,bound_at=excluded.bound_at'''),
        dict(chat=chat_id,user=user.id,title=str(title or '')[:255],actor=actor,now=now))
    return dict(chat_id=chat_id,user_id=user.id,title=str(title or '')[:255])


def unbind(session,user_id,chat_id):
    session.execute(text('UPDATE telegram_account_groups SET active=0 WHERE chat_id=:chat AND user_id=:user'),dict(chat=str(chat_id),user=int(user_id)))


def claim_message(session,chat_id,message_id,sender_id,user_id):
    unknown=session.execute(text("SELECT 1 FROM telegram_message_events WHERE namespace='legacy:unknown' AND chat_id=:chat AND message_id=:message"),
        dict(chat=str(chat_id),message=int(message_id))).scalar()
    if unknown:
        return False
    result=session.execute(text('''INSERT INTO telegram_message_events(namespace,chat_id,message_id,sender_id,user_id,status,created_at)
       VALUES(:namespace,:chat,:message,:sender,:user,'claimed',:now) ON CONFLICT(namespace,chat_id,message_id) DO NOTHING'''),
       parameters(chat_id,message=int(message_id),sender=str(sender_id),user=user_id,now=datetime.now(timezone.utc).isoformat()))
    return result.rowcount==1


def new_draft(session,chat_id,message_id,sender_id,user_id,payload,waiting_field):
    token=secrets.token_urlsafe(16);now=datetime.now(timezone.utc).isoformat()
    session.execute(text('''INSERT INTO telegram_order_drafts(namespace,token,chat_id,message_id,sender_id,user_id,payload,waiting_field,created_at,updated_at)
      VALUES(:namespace,:token,:chat,:message,:sender,:user,:payload,:waiting,:now,:now)'''),
      parameters(chat_id,token=token,message=message_id,sender=str(sender_id),user=user_id,
           payload=json.dumps(payload,ensure_ascii=False),waiting=waiting_field,now=now))
    session.execute(text('UPDATE telegram_message_events SET draft_token=:token WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message'),parameters(chat_id,token=token,message=message_id))
    return token


def select_draft(session,chat_id,user_id,sender_id,reply_message_id=None,token=None):
    sql='SELECT * FROM telegram_order_drafts WHERE namespace=:namespace AND chat_id=:chat AND user_id=:user AND status=\'pending\''
    args=parameters(chat_id,user=user_id)
    from telegram_source_identity import account_cutover
    boundary=account_cutover()
    if boundary is not None:
        sql+=' AND created_at>=:account_cutover'
        args['account_cutover']=boundary.isoformat()
    if token:
        sql+=' AND token=:token';args['token']=token
    elif reply_message_id is not None:
        sql+=' AND (message_id=:reply OR token IN (SELECT draft_token FROM telegram_message_events WHERE namespace=:namespace AND chat_id=:chat AND user_id=:user AND message_id=:reply))';args['reply']=reply_message_id
    else:
        sql+=' AND sender_id=:sender';args['sender']=str(sender_id)
    rows=session.execute(text(sql),args).mappings().all()
    if len(rows)>1:raise ValueError('存在多张未完成订单，请回复对应订单消息；不能猜测补充目标')
    return dict(rows[0]) if rows else None


def open_message_draft(session, chat_id, message_id, sender_id, user_id,
                       new_order=False, reply_message_id=None, resume=False):
    """Claim and resolve a message in one transaction owned by the caller.

    The unique message insert serializes competing SQLite writers. Ambiguous
    replies raise before commit, so a failed resolution never consumes a message.
    A repeated Telegram delivery returns no draft and never runs the Parser.
    """
    if not claim_message(session, chat_id, message_id, sender_id, user_id):
        if resume:
            row=session.execute(text('''SELECT d.* FROM telegram_message_events e
                JOIN telegram_order_drafts d ON d.token=e.draft_token
                WHERE e.namespace=:namespace AND d.namespace=e.namespace AND e.chat_id=:chat AND e.message_id=:message
                AND e.sender_id=:sender AND e.user_id=:user
                AND d.user_id=:user AND d.status='pending' '''),
                parameters(chat_id,message=message_id,sender=str(sender_id),user=user_id)).mappings().first()
            return dict(row) if row else None
        return None
    session.execute(text('UPDATE telegram_account_groups SET last_message_at=:time WHERE chat_id=:chat AND active=1'),dict(chat=str(chat_id),time=datetime.now(timezone.utc).isoformat()))
    draft = (select_draft(session, chat_id, user_id, sender_id,
                          reply_message_id=reply_message_id)
             if not new_order or reply_message_id is not None else None)
    if draft is None:
        if not new_order:
            return None
        token = new_draft(session, chat_id, message_id, sender_id, user_id, {}, None)
        draft = select_draft(session, chat_id, user_id, sender_id, token=token)
    session.execute(text('UPDATE telegram_message_events SET draft_token=:token '
                         'WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message'),
                    parameters(chat_id,token=draft['token'], message=message_id))
    return draft


def notification_target(session,user_id):
    from notification_routing import resolve_notification_target
    return resolve_notification_target(session,user_id,text)


def payment_notice_key(session,key,user_id,kind):
    """Payment IDs may be reused after an externally authorized data reset.

    Use immutable row identity, retaining demonstrably current legacy events.
    This never changes a payment, balance or historical notification.
    """
    match=re.fullmatch(r'payment:(\d+)',str(key))
    if kind not in {'payment','prepaid'} or not match:return key
    from models import Payment
    payment=session.get(Payment,int(match.group(1)))
    if payment is None:return key  # legacy/synthetic non-row event compatibility
    if payment.user_id!=user_id:raise ValueError('收款通知账户与收款记录不一致')
    stamp=payment.created_at.replace(tzinfo=timezone.utc) if payment.created_at.tzinfo is None else payment.created_at.astimezone(timezone.utc)
    digest=hashlib.sha256(f'{payment.id}:{user_id}:{stamp.isoformat()}'.encode()).hexdigest()[:32]
    canonical=f'payment-v2:{payment.id}:{user_id}:{digest}'
    old=session.execute(text('SELECT user_id,kind,status,created_at FROM telegram_notification_events WHERE event_key=:key'),dict(key=key)).mappings().first()
    if old and old['status']!='canonical_alias' and old['user_id']==user_id and old['kind']==kind:
        try:
            created=datetime.fromisoformat(old['created_at'])
            created=created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created.astimezone(timezone.utc)
            if stamp<=created<=stamp+timedelta(days=1):return key
        except (TypeError,ValueError):pass
    return canonical


def queue_notice(session,key,user_id,kind,payload):
    key=payment_notice_key(session,key,user_id,kind)
    target=notification_target(session,user_id)
    if target is None:raise ValueError('通知账户不存在')
    from account_notifications import prepare_bill_payload
    payload=prepare_bill_payload(session,user_id,kind,payload)
    result=session.execute(text('''INSERT INTO telegram_notification_events(event_key,user_id,chat_id,kind,payload,created_at)
       VALUES(:key,:user,:chat,:kind,:payload,:now) ON CONFLICT(event_key) DO NOTHING'''),
       dict(key=key,user=user_id,chat=target,kind=kind,payload=json.dumps(payload,ensure_ascii=False),now=datetime.now(timezone.utc).isoformat()))
    canonical=re.fullmatch(r'payment-v2:(\d+):(\d+):[0-9a-f]{32}',str(key))
    if canonical:
        # An older code rollback claims only pending legacy keys. A non-sendable
        # alias reserves that key when absent, preventing duplicate delivery.
        # Existing historical collisions are left exactly unchanged.
        session.execute(text('''INSERT INTO telegram_notification_events
            (event_key,user_id,chat_id,kind,payload,status,created_at)
            VALUES(:key,:user,:chat,:kind,:payload,'canonical_alias',:now)
            ON CONFLICT(event_key) DO NOTHING'''),dict(key='payment:'+canonical.group(1),
            user=user_id,chat=target,kind=kind,payload=json.dumps({'canonical_event_key':key}),
            now=datetime.now(timezone.utc).isoformat()))
    return result.rowcount==1


def search_orders(session,user_id,keyword='',offset=0,limit=5):
    keyword=str(keyword or '').strip()
    if len(keyword)>120:raise ValueError('搜索关键词过长')
    # Account restriction is part of the database query, never a UI filter.
    query=session.query(Order).filter(Order.user_id==user_id)
    if keyword:
        pattern='%'+keyword.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        query=query.filter(or_(Order.recipient_name.like(pattern,escape='\\'),
            Order.recipient_phone.like(pattern,escape='\\'),Order.recipient_address.like(pattern,escape='\\')))
    return query.order_by(Order.created_at.desc(),Order.id.desc()).offset(max(0,int(offset))).limit(min(10,max(1,int(limit)))).all()


def receipt_daily_summary(session,user_id,now=None,business_timezone='Asia/Shanghai'):
    """Only committed daily receipt facts; no history/bill/manual-ledger scan.

    The full account summary remains unchanged for bills and account menus.
    Keep float/round and logical split-count semantics identical to that view.
    """
    if session.get(User,user_id) is None:raise ValueError('客户不存在')
    zone=ZoneInfo(business_timezone)
    now=(now or datetime.now(timezone.utc)).astimezone(zone)
    today=now.replace(hour=0,minute=0,second=0,microsecond=0)
    utc=lambda value:value.astimezone(timezone.utc).replace(tzinfo=None)
    rows=session.query(Order.id,Order.split_group_id,Order.grand_total).filter(
        Order.user_id==user_id,Order.status!='cancelled',
        Order.created_at>=utc(today),Order.created_at<=utc(now)).order_by(Order.id).all()
    logical={('split',row.split_group_id) if row.split_group_id else ('order',row.id) for row in rows}
    return dict(today_count=len(logical),today_amount=round(sum(float(row.grand_total or 0) for row in rows),2))


def summary(session,user_id,now=None,business_timezone='Asia/Shanghai'):
    user=session.get(User,user_id)
    if user is None:raise ValueError('客户不存在')
    zone=ZoneInfo(business_timezone)
    now=(now or datetime.now(timezone.utc)).astimezone(zone)
    today=now.replace(hour=0,minute=0,second=0,microsecond=0)
    week=today-timedelta(days=today.weekday())
    utc=lambda value:value.astimezone(timezone.utc).replace(tzinfo=None)
    orders=session.query(Order).filter(Order.user_id==user_id,Order.status!='cancelled').all()
    today_orders=[o for o in orders if o.created_at and utc(today)<=o.created_at<=utc(now)]
    weekly=[o for o in orders if o.created_at and utc(week)<=o.created_at<=utc(now)]
    money=lambda values:round(sum(float(x or 0) for x in values),2)
    logical_orders={('split',o.split_group_id) if o.split_group_id else ('order',o.id) for o in today_orders}
    from manual_receivables import outstanding as manual_outstanding
    manual_pending=manual_outstanding(session,user_id)
    manual_settled=float(session.execute(text('SELECT COALESCE(SUM(paid_cents),0) FROM manual_receivables WHERE user_id=:user'),dict(user=user_id)).scalar() or 0)/100
    return dict(prepaid=round(float(user.balance or 0),2),today_count=len(logical_orders),
        today_amount=money(o.grand_total for o in today_orders),week_amount=money(o.grand_total for o in weekly),
        settled=round(money(o.paid_amount for o in orders)+manual_settled,2),
        pending=round(money(max(0,float(o.grand_total or 0)-float(o.paid_amount or 0)) for o in orders)+manual_pending,2))
