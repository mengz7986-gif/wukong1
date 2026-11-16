"""Future-only cancellation outbox consumer; never imports business models.

Only notification delivery metadata is writable. An interrupted/ambiguous send
is deliberately never retried. Historical events remain completely untouched.
"""
import html
import json
import sqlite3
from datetime import datetime, timezone


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('CANCEL_NOTICE_TIMEZONE_REQUIRED')
    return parsed.astimezone(timezone.utc)


def error_category(error):
    """Controlled diagnostics only: never log SQL, payload or exception text."""
    if isinstance(error, sqlite3.OperationalError):
        code = getattr(error, 'sqlite_errorcode', None)
        if code is not None and code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return 'database_busy'
        # Python 3.10 does not expose sqlite_errorcode.
        if str(error).lower() in ('database is locked', 'database table is locked'):
            return 'database_busy'
        return 'database_operational_error'
    if isinstance(error, PermissionError):
        return 'permission_error'
    if isinstance(error, ValueError):
        return 'validation_error'
    return 'worker_error'


def authorize(action, table, column, database_name, trigger):
    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_UPDATE:
        return sqlite3.SQLITE_OK if table == 'telegram_notification_events' and column in {
            'status', 'attempts', 'last_error', 'sent_at'} else sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
                  sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def claim(connection, cutoff):
    boundary = timestamp(cutoff)
    connection.row_factory = sqlite3.Row
    # Empty polling is read-only. In particular, never contend for the order
    # writer lock just to discover that there is nothing to send.
    available = connection.execute('''SELECT 1 FROM telegram_notification_events
        WHERE kind='cancelled' AND status='pending' AND attempts=0
        AND julianday(created_at)>julianday(?) LIMIT 1''', (cutoff,)).fetchone()
    if not available:
        return None
    connection.execute('BEGIN IMMEDIATE')
    try:
        rows = connection.execute('''SELECT n.*, f.cancelled_at, f.reason,
            o.order_number, o.source_chat_id, o.source_message_id,
            u.telegram_id
            FROM telegram_notification_events n
            JOIN order_cancellation_facts f ON n.event_key='cancel:'||f.event_id
            JOIN orders o ON o.id=f.order_id AND o.user_id=n.user_id
            JOIN users u ON u.id=o.user_id
            WHERE n.kind='cancelled' AND n.status='pending' AND n.attempts=0
            AND o.status='cancelled'
            AND julianday(n.created_at)>julianday(?)
            AND julianday(f.cancelled_at)>julianday(?)
            ORDER BY n.created_at,n.event_key LIMIT 32''', (cutoff, cutoff)).fetchall()
        for row in rows:
            from cancel_notice_hold import held, guard_event, CancellationReplayHeld
            if held(row['event_key']):
                try:guard_event(row['event_key'], 'cancel_claim')
                except CancellationReplayHeld:continue
            if timestamp(row['created_at']) <= boundary or timestamp(row['cancelled_at']) <= boundary:
                continue
            payload = json.loads(row['payload'])
            expected = '\n'.join(['❌ 订单已取消', '',
                '订单号：' + html.escape(str(row['order_number'])),
                '取消原因：' + html.escape(row['reason'])])
            target = str(row['source_chat_id'] or row['telegram_id'] or '')
            source = row['source_message_id'] if row['source_chat_id'] else None
            if (not target or str(row['chat_id']) != target or payload.get('text') != expected
                    or payload.get('reply_to_message_id') != (source if isinstance(source, int) and source > 0 else None)):
                raise ValueError('CANCEL_NOTICE_FACT_MISMATCH')
            changed = connection.execute('''UPDATE telegram_notification_events
                SET status='sending',attempts=attempts+1
                WHERE event_key=? AND status='pending' AND attempts=0''', (row['event_key'],)).rowcount
            if changed:
                connection.commit()
                outgoing = {'chat_id': target, 'text': expected, 'parse_mode': 'HTML'}
                if payload.get('reply_to_message_id') is not None:
                    outgoing['reply_to_message_id'] = payload['reply_to_message_id']
                return row['event_key'], outgoing
        connection.commit()
        return None
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
            from cancel_notice_hold import sending
            with sending(key):
                ok, detail = send(payload)
            if not ok and detail == 'telegram_reply_unavailable' and 'reply_to_message_id' in payload:
                payload.pop('reply_to_message_id')
                fallback = True
                with sending(key):
                    ok, detail = send(payload)
        except Exception:
            ok, detail = False, 'delivery_unknown'
        definitive = detail in {'telegram_http_400', 'telegram_http_401', 'telegram_http_403',
            'telegram_http_404', 'telegram_http_422', 'telegram_reply_unavailable',
            'telegram_retry_after_exhausted', 'telegram_delivery_queue_full'}
        status = 'sent' if ok else 'failed' if definitive else 'unknown'
        error = ('source_message_unavailable' if fallback else None) if ok else detail if definitive else 'delivery_unknown'
        connection.execute('''UPDATE telegram_notification_events
            SET status=?,last_error=?,sent_at=?
            WHERE event_key=? AND status='sending' AND attempts=1''',
            (status, error, datetime.now(timezone.utc).isoformat() if ok else None, key))
        connection.commit()
        return status
    finally:
        end_activity(activity)


def tick(connection, cutoff, send):
    """Transient storage contention defers metadata work, never replays a send.

    A send was preceded by a committed 'sending' claim. If saving its result
    fails, that claim stays frozen; the next tick cannot dispatch it again.
    """
    try:
        return deliver_one(connection, cutoff, send)
    except Exception as error:
        connection.rollback()
        if error_category(error) == 'database_busy':
            return 'deferred_database_busy'
        raise
