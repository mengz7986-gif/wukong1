"""Resolve account notices from group identity, independent of order status."""
import re


def resolve_notification_target(session, user_id, sql_text):
    user = session.execute(sql_text('SELECT telegram_id FROM users WHERE id=:user'),
                           {'user': user_id}).fetchone()
    if user is None:
        return None
    bindings = session.execute(sql_text('''SELECT chat_id FROM telegram_account_groups
        WHERE user_id=:user AND active=1'''), {'user': user_id}).fetchall()
    if bindings:
        targets = {str(row[0]) for row in bindings}
        if len(targets) != 1 or not re.fullmatch(r'-[1-9]\d*', next(iter(targets))):
            raise ValueError('通知接单群绑定不唯一或无效，请核对群绑定')
        return next(iter(targets))
    # Cancellation changes business state, never the identity of the source
    # group. An explicitly unbound/reassigned group must not be resurrected.
    group = session.execute(sql_text('''SELECT o.source_chat_id FROM orders o
        WHERE o.user_id=:user AND o.source_chat_type IN ('group','supergroup')
        AND o.source_chat_id LIKE '-%'
        AND NOT EXISTS (SELECT 1 FROM telegram_account_groups g
            WHERE g.chat_id=o.source_chat_id AND (g.user_id<>:user OR g.active<>1))
        ORDER BY o.created_at DESC,o.id DESC LIMIT 1'''), {'user': user_id}).fetchone()
    if group:
        target = str(group[0])
        if not re.fullmatch(r'-[1-9]\d*', target):
            raise ValueError('历史订单的来源群无效，请核对群绑定')
        return target
    # Preserve private-only accounts that have no usable group history.
    return str(user[0]) if user[0] else None
