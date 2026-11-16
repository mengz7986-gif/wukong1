"""Non-order receivables: cents, audit and outbox committed together."""
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import hashlib
import html
import json
import re
from sqlalchemy import text

def migrate(connection):
    connection.execute(text('''CREATE TABLE IF NOT EXISTS manual_receivables (
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
        amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
        paid_cents INTEGER NOT NULL DEFAULT 0 CHECK(paid_cents>=0 AND paid_cents<=amount_cents),
        reason TEXT NOT NULL, operator TEXT NOT NULL, created_at TEXT NOT NULL,
        operation_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL)'''))
    connection.execute(text('''CREATE TABLE IF NOT EXISTS manual_receivable_settlements (
        id INTEGER PRIMARY KEY, receivable_id INTEGER NOT NULL REFERENCES manual_receivables(id),
        payment_id INTEGER NOT NULL REFERENCES payments(id),
        amount_cents INTEGER NOT NULL CHECK(amount_cents>0), created_at TEXT NOT NULL,
        UNIQUE(receivable_id,payment_id))'''))
    connection.execute(text('CREATE INDEX IF NOT EXISTS ix_manual_receivables_user ON manual_receivables(user_id,id)'))
    connection.execute(text('''CREATE TABLE IF NOT EXISTS manual_receivable_notice_retries (
        operation_key TEXT PRIMARY KEY, receivable_id INTEGER NOT NULL REFERENCES manual_receivables(id),
        operator TEXT NOT NULL, reason TEXT NOT NULL, previous_error TEXT NOT NULL,
        chat_id TEXT NOT NULL, created_at TEXT NOT NULL)'''))

def cents(value):
    try:
        number=Decimal(str(value))
        if not number.is_finite() or number<=0 or number>Decimal('10000000') or number.as_tuple().exponent < -2:
            raise ValueError('金额必须大于0、最多两位小数，且不超过1000万元')
        return int(number*100)
    except (InvalidOperation,TypeError):
        raise ValueError('请输入有效金额') from None

def outstanding(session,user_id=None):
    sql='SELECT COALESCE(SUM(amount_cents-paid_cents),0) FROM manual_receivables'
    if user_id is not None:sql+=' WHERE user_id=:user'
    return int(session.execute(text(sql),dict(user=user_id)).scalar() or 0)/100

def add(database,user_id,amount,reason,operator,operation_key):
    amount_cents=cents(amount)
    reason=str(reason or '').strip();operator=str(operator or '').strip()
    if not reason or len(reason)>500:raise ValueError('款项缘由必填，最多500字')
    if not operator:raise ValueError('缺少操作人')
    if not isinstance(operation_key,str) or not 16<=len(operation_key)<=100:
        raise ValueError('缺少有效防重复提交编号')
    request_hash=hashlib.sha256(json.dumps([user_id,amount_cents,reason,operator],ensure_ascii=False).encode()).hexdigest()
    with database.SessionLocal() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        old=session.execute(text('SELECT id,request_hash FROM manual_receivables WHERE operation_key=:key'),dict(key=operation_key)).mappings().first()
        if old:
            if old['request_hash']!=request_hash:raise ValueError('提交编号已用于不同款项；请重新打开操作')
            return dict(receivable_id=old['id'],duplicate=True,user_debt=database.calc_user_debt(user_id,session=session))
        from models import User
        user=session.get(User,user_id)
        if user is None or not user.is_active:raise ValueError('用户不存在或已停用')
        from telegram_accounts import queue_notice,notification_target
        target=notification_target(session,user_id)
        if not target or not re.fullmatch(r'-?[1-9]\d*',target):raise ValueError('缺少该用户可核对的通知地址')
        from account_notifications import bill_owner_mention
        mention=bill_owner_mention(user)
        created=datetime.now(timezone.utc).isoformat()
        row=session.execute(text('''INSERT INTO manual_receivables
            (user_id,amount_cents,reason,operator,created_at,operation_key,request_hash)
            VALUES(:user,:amount,:reason,:operator,:created,:key,:hash)'''),
            dict(user=user_id,amount=amount_cents,reason=reason,operator=operator,created=created,key=operation_key,hash=request_hash))
        identifier=row.lastrowid;debt=database.calc_user_debt(user_id,session=session)
        from cumulative_bill_snapshot import capture
        message=capture(session,user_id,note='增加欠款 ¥%.2f；款项缘由：%s' % (amount_cents/100,reason))['text']
        queue_notice(session,'receivable:'+str(identifier),user_id,'receivable',dict(text=message))
        session.commit()
        return dict(receivable_id=identifier,duplicate=False,amount=amount_cents/100,user_debt=debt,notification_status='pending')

def allocate(session,user_id,remaining):
    """Account-level receipts settle orders first, then these obligations."""
    budget=int(Decimal(str(round(remaining,2)))*100);allocations=[]
    for row in session.execute(text('''SELECT id,amount_cents-paid_cents AS due FROM manual_receivables
            WHERE user_id=:user AND paid_cents<amount_cents ORDER BY id'''),dict(user=user_id)).mappings():
        used=min(budget,row['due'])
        if used<=0:break
        session.execute(text('UPDATE manual_receivables SET paid_cents=paid_cents+:used WHERE id=:id'),dict(id=row['id'],used=used))
        budget-=used;allocations.append(dict(receivable_id=row['id'],paid=used/100))
    return budget/100,allocations

def record_allocations(session,payment_id,allocations):
    for allocation in allocations:
        session.execute(text('''INSERT INTO manual_receivable_settlements
            (receivable_id,payment_id,amount_cents,created_at) VALUES(:id,:payment,:amount,:created)'''),
            dict(id=allocation['receivable_id'],payment=payment_id,amount=int(round(allocation['paid']*100)),created=datetime.now(timezone.utc).isoformat()))

def retry_notice(database,identifier,operator,reason,operation_key):
    """Requeue one provably unsent notice; never recreate its obligation."""
    operator=str(operator or '').strip();reason=str(reason or '').strip()
    if not operator or not reason or len(reason)>500:raise ValueError('操作人与恢复缘由必填，缘由最多500字')
    if not isinstance(operation_key,str) or not 16<=len(operation_key)<=100:raise ValueError('缺少有效防重复编号')
    with database.SessionLocal() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        prior=session.execute(text('SELECT * FROM manual_receivable_notice_retries WHERE operation_key=:key'),dict(key=operation_key)).mappings().first()
        if prior:
            if (prior['receivable_id'],prior['operator'],prior['reason'])!=(identifier,operator,reason):
                raise ValueError('恢复编号已用于不同操作')
            return dict(receivable_id=identifier,duplicate=True,business_writes=0)
        event=session.execute(text('''SELECT n.* FROM telegram_notification_events n JOIN manual_receivables r
            ON n.event_key='receivable:'||r.id AND n.user_id=r.user_id WHERE r.id=:id AND n.kind='receivable' '''),dict(id=identifier)).mappings().first()
        allowed={'telegram_http_400','telegram_http_401','telegram_http_403','telegram_http_404','telegram_http_422',
                 'telegram_delivery_queue_full','telegram_retry_after_exhausted','telegram_reply_unavailable',
                 'BOT token missing','chat_id missing'}
        if not event or event['status']!='failed' or event['last_error'] not in allowed:
            raise ValueError('通知不是已证明未送达状态；结果未知或历史普通失败不得自动重发')
        from telegram_accounts import notification_target
        if notification_target(session,event['user_id'])!=event['chat_id']:
            raise ValueError('通知归属地址已变化；须人工核对，不能改发到其他群')
        session.execute(text('''INSERT INTO manual_receivable_notice_retries
            (operation_key,receivable_id,operator,reason,previous_error,chat_id,created_at)
            VALUES(:key,:id,:operator,:reason,:error,:chat,:created)'''),dict(key=operation_key,id=identifier,
                operator=operator,reason=reason,error=event['last_error'],chat=event['chat_id'],created=datetime.now(timezone.utc).isoformat()))
        session.execute(text("UPDATE telegram_notification_events SET status='pending',last_error=NULL WHERE event_key=:key AND status='failed'"),dict(key=event['event_key']))
        session.commit()
        return dict(receivable_id=identifier,duplicate=False,business_writes=0,notification_status='pending')
