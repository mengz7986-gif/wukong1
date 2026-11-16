"""Message-box identity, separate from physical Telegram chat identifiers.

Never accepts a caller-supplied bot ID. Unknown historical provenance stays
quarantined; changing the observing bot must not replay historical orders.
"""
from datetime import datetime, timezone
import os


def account_cutover():
    """Explicit current-account boundary; absent means legacy behavior unchanged."""
    if os.getenv('NEWBOT_INGESTION_POLICY_PATH'):
        from newbot_safety import policy
        return policy()['_boundary']
    value = os.getenv('BOT_ACCOUNT_SWITCH_AT', '')
    if not value:
        return None
    if os.getenv('BOT_ACCOUNT_SWITCH_ID') != bot_id():
        raise ValueError('BOT_SWITCH_OBSERVER_MISMATCH')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('BOT_SWITCH_BOUNDARY_REQUIRES_TIMEZONE')
    return result.astimezone(timezone.utc)


def filter_current_source_orders(query, chat_id):
    boundary = account_cutover()
    if boundary is None or scope(chat_id) == 'shared':
        return query
    from models import Order
    from sqlalchemy import or_
    # Null/unknown historical provenance must still block rather than be guessed.
    return query.filter(or_(Order.created_at.is_(None),
                           Order.created_at >= boundary.replace(tzinfo=None)))


def source_order_exists(connection, namespace, chat_id, message_id):
    from sqlalchemy import text
    sql = '''SELECT 1 FROM orders WHERE source_chat_id=:chat
        AND (source_message_id=:message OR source_message_id IN (
            SELECT d.message_id FROM telegram_message_events e
            JOIN telegram_order_drafts d ON d.token=e.draft_token
            WHERE e.namespace=:namespace AND d.namespace=e.namespace
            AND e.chat_id=:chat AND e.message_id=:message))'''
    values = dict(namespace=namespace, chat=str(chat_id), message=message_id)
    boundary = account_cutover()
    if boundary is not None and namespace == scope(chat_id) and namespace != 'shared':
        sql += ' AND (orders.created_at IS NULL OR orders.created_at>=:account_cutover)'
        values['account_cutover'] = boundary.replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S.%f')
    return bool(connection.execute(text(sql+' LIMIT 1'),values).scalar())


def bot_id():
    from config import BOT_TOKEN
    value = str(BOT_TOKEN or '').partition(':')[0]
    if not value.isdigit():
        raise ValueError('MESSAGE_OBSERVER_ID_UNAVAILABLE')
    return value


def scope(chat_id, observer=None):
    chat = int(chat_id)
    # Bot API channel/supergroup peer range; not the textual "-100" prefix.
    if chat <= -1000000000000:
        return 'shared'
    observer = str(observer if observer is not None else bot_id())
    if not observer.isdigit():
        raise ValueError('MESSAGE_OBSERVER_ID_INVALID')
    return 'bot:' + observer


def historical_scope(chat_id, created_at, old_bot, new_bot, cutover):
    if scope(chat_id, old_bot) == 'shared':
        return 'shared'
    try:
        value = datetime.fromisoformat(str(created_at).replace('Z', '+00:00'))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        boundary = datetime.fromisoformat(cutover.replace('Z', '+00:00'))
        if boundary.tzinfo is None:
            raise ValueError('UNZONED_CUTOVER')
    except (TypeError, ValueError):
        return 'legacy:unknown'
    return scope(chat_id, old_bot if value < boundary else new_bot)


def parameters(chat_id, **values):
    return dict(values, chat=str(chat_id), namespace=scope(chat_id))
