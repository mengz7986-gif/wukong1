"""Cancellation evidence and its notice, committed with the existing rollback."""
import html
import json
import uuid
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.orm import object_session


def migrate(connection):
    connection.execute(text('''CREATE TABLE IF NOT EXISTS order_cancellation_facts (
        event_id TEXT PRIMARY KEY, order_id INTEGER NOT NULL,
        reason TEXT NOT NULL, cancelled_at TEXT NOT NULL)'''))
    connection.execute(text('CREATE INDEX IF NOT EXISTS ix_cancellation_order ON order_cancellation_facts(order_id,cancelled_at)'))


def record(session, order, reason, user):
    now = datetime.now(timezone.utc).isoformat()
    event = uuid.uuid4().hex
    session.execute(text('''INSERT INTO order_cancellation_facts
        (event_id,order_id,reason,cancelled_at) VALUES(:event,:order,:reason,:now)'''),
        {'event': event, 'order': order.id, 'reason': reason, 'now': now})
    # Queue only after the normal cancel/FIFO/refund writes, in their same
    # transaction. Delivery is performed separately and never rolls them back.
    available = session.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_notification_events'")).scalar()
    if available and user and user.telegram_id:
        target = str(order.source_chat_id or user.telegram_id)
        message = '\n'.join(['❌ 订单已取消', '',
            '订单号：' + html.escape(str(order.order_number)),
            '取消原因：' + html.escape(reason)])
        payload={'text':message}
        source_id=getattr(order,'source_message_id',None)
        if order.source_chat_id and isinstance(source_id,int) and not isinstance(source_id,bool) and source_id>0:
            payload['reply_to_message_id']=source_id
        else:
            payload['reply_fallback']='source_message_missing'
        session.execute(text('''INSERT INTO telegram_notification_events
            (event_key,user_id,chat_id,kind,payload,created_at)
            VALUES(:key,:user,:chat,'cancelled',:payload,:now)'''),
            {'key': 'cancel:' + event, 'user': user.id, 'chat': target,
             'payload': json.dumps(payload, ensure_ascii=False), 'now': now})


def latest(order):
    if getattr(order,'status',None)!='cancelled' or not hasattr(order,'_sa_instance_state'):
        return {}
    session=object_session(order)
    if session is None:return {}
    row=session.execute(text('''SELECT reason,cancelled_at FROM order_cancellation_facts
        WHERE order_id=:id ORDER BY cancelled_at DESC,event_id DESC LIMIT 1'''),
        {'id':order.id}).mappings().first()
    return {'cancellation_reason':row['reason'],'cancelled_at':row['cancelled_at']} if row else {}
