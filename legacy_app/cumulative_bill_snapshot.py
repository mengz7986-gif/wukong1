"""Capture current ledger facts inside the caller's transaction, never replay.

No historical as-of reconstruction is promised: order.paid_amount is mutable.
Callers persist this result once and reuse it for delayed notification retries.
"""
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import text

def capture(session, user_id, note=None, *, snapshot_time=None):
    connection = session.connection()
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN')
    from models import User, Order
    from account_notifications import bill_owner_mention
    from cumulative_bill_format import render
    user = session.get(User, user_id)
    if user is None:
        raise ValueError('账单所属用户不存在')
    if user.balance is None:
        raise ValueError('预存余额资料缺失，需核对后再推送，禁止猜金额')
    cutoff = snapshot_time or datetime.now(timezone.utc)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError('账单截止时间必须带时区')
    total = Decimal('0'); paid = Decimal('0')
    relevant=session.query(Order).filter(Order.user_id == user_id, Order.status != 'cancelled')
    if relevant.filter(Order.created_at.is_(None)).first():
        raise ValueError('存在时间归属不明的账单，需核对后再推送')
    for order in relevant.filter(Order.created_at <= cutoff.astimezone(timezone.utc).replace(tzinfo=None)).all():
        if order.grand_total is None or order.paid_amount is None:
            raise ValueError('订单货款或已结金额资料缺失，禁止猜测累计账单')
        amount = Decimal(str(order.grand_total or 0))
        settled = Decimal(str(order.paid_amount or 0))
        if not amount.is_finite() or not settled.is_finite() or amount < 0 or settled < 0 or settled > amount:
            raise ValueError('订单账务金额异常，需人工核对，禁止猜测累计账单')
        total += amount; paid += settled
    for row in session.execute(text('SELECT amount_cents,paid_cents FROM manual_receivables WHERE user_id=:user AND created_at<=:cutoff'), {'user':user_id,'cutoff':cutoff.isoformat()}):
        if row.amount_cents < 0 or row.paid_cents < 0 or row.paid_cents > row.amount_cents:
            raise ValueError('手工欠款账务金额异常，需人工核对，禁止猜测累计账单')
        total += Decimal(row.amount_cents)/100
        paid += Decimal(row.paid_cents)/100
    facts = dict(current=str(total), settled=str(paid), prepaid=str(user.balance or 0),
                 pending=str(total-paid), cutoff=cutoff.isoformat(), user_id=user_id)
    facts['text'] = render(bill_owner_mention(user), cutoff, current=total, settled=paid,
                           prepaid=user.balance or 0, pending=total-paid, note=note)
    return facts
