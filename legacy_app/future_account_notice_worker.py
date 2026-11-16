"""Future-only account notices without importing or executing business models.

Claims share the API's pending-to-sending transition. Only delivery metadata
may be written; ambiguous/interrupted delivery is never replayed.
"""
import json
import re
import sqlite3

from cancel_notice_worker import error_category, timestamp


KINDS = ('receivable', 'payment', 'prepaid', 'manual_bill')
PREDICATE = """kind IN ('receivable','payment','prepaid','manual_bill')
    AND status='pending' AND attempts=0 AND julianday(created_at)>julianday(?)"""


def claim(connection, cutoff):
    timestamp(cutoff)
    connection.row_factory = sqlite3.Row
    if not connection.execute(
            'SELECT 1 FROM telegram_notification_events WHERE '+PREDICATE+' LIMIT 1',
            (cutoff,)).fetchone():
        return None
    connection.execute('BEGIN IMMEDIATE')
    try:
        row = connection.execute(
            'SELECT event_key,chat_id,payload FROM telegram_notification_events WHERE '
            +PREDICATE+' ORDER BY created_at,event_key LIMIT 1', (cutoff,)).fetchone()
        if row is None:
            connection.commit()
            return None
        payload = json.loads(row['payload'])
        target = str(row['chat_id'] or '')
        message = payload.get('text')
        if not re.fullmatch(r'-?[1-9]\d*', target) or not isinstance(message, str) or not message.strip():
            raise ValueError('ACCOUNT_NOTICE_PAYLOAD_INVALID')
        outgoing = dict(chat_id=target, text=message, parse_mode='HTML')
        reply = payload.get('reply_to_message_id')
        if reply is not None:
            if type(reply) is not int or reply <= 0:
                raise ValueError('ACCOUNT_NOTICE_REPLY_INVALID')
            outgoing['reply_to_message_id'] = reply
        changed = connection.execute('''UPDATE telegram_notification_events
            SET status='sending',attempts=attempts+1
            WHERE event_key=? AND status='pending' AND attempts=0''',
            (row['event_key'],)).rowcount
        connection.commit()
        return (row['event_key'], outgoing) if changed else None
    except BaseException:
        connection.rollback()
        raise


def deliver_one(connection, cutoff, send):
    from deployment_drain import begin_activity, end_activity
    activity = begin_activity()
    if activity is None:
        return 'paused'
    try:
        event = claim(connection, cutoff)
        if event is None:
            return 'idle'
        key, payload = event
        fallback = False
        try:
            ok, detail = send(payload)
            if not ok and detail == 'telegram_reply_unavailable' and 'reply_to_message_id' in payload:
                payload.pop('reply_to_message_id')
                fallback = True
                ok, detail = send(payload)
        except Exception:
            ok, detail = False, 'delivery_unknown'
        definitive = detail in {
            'telegram_http_400', 'telegram_http_401', 'telegram_http_403',
            'telegram_http_404', 'telegram_http_422', 'telegram_reply_unavailable',
            'telegram_retry_after_exhausted', 'telegram_delivery_queue_full'}
        status = 'sent' if ok else 'failed' if definitive else 'unknown'
        error = ('source_message_unavailable' if fallback else None) if ok else detail if definitive else 'delivery_unknown'
        from datetime import datetime, timezone
        connection.execute('''UPDATE telegram_notification_events
            SET status=?,last_error=?,sent_at=?
            WHERE event_key=? AND status='sending' AND attempts=1''',
            (status, error, datetime.now(timezone.utc).isoformat() if ok else None, key))
        connection.commit()
        return status
    finally:
        end_activity(activity)


def tick(connection, cutoff, send):
    try:
        return deliver_one(connection, cutoff, send)
    except Exception as error:
        connection.rollback()
        if error_category(error) == 'database_busy':
            return 'deferred_database_busy'
        raise
