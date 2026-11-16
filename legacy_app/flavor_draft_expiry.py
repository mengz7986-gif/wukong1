"""Expire only unrecorded flavor-confirmation drafts; keep their audit payload."""
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import text

MESSAGE = '订单确认已超时，请重新下单。'
CONFIRMATION_TIMEOUT_HOURS = 2


def expire(session, now=None, chat_id=None, user_id=None):
    now = now or datetime.now(timezone.utc)
    sql = "SELECT namespace,token,chat_id,message_id,waiting_field,payload,updated_at FROM telegram_order_drafts WHERE status='pending'"
    args = {}
    for field, value in [('chat_id', chat_id), ('user_id', user_id)]:
        if value is not None:
            sql += ' AND '+field+'=:'+field
            args[field] = value
    count = 0
    for row in session.execute(text(sql), args).mappings():
        try:
            payload = json.loads(row['payload'])
            pending = payload.get('pending_ai_flavor') or payload.get('pending_order')
            if not pending or not (payload.get('pending_ai_flavor') or row['waiting_field']=='confirmation'):
                continue
            started = datetime.fromisoformat(pending.get('_confirmation_started_at') or row['updated_at'])
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            # Pre-cutover shared drafts are immutable historical evidence.
            from telegram_source_identity import account_cutover
            cutover = account_cutover()
            if row['namespace'] == 'shared' and cutover and started < cutover:
                continue
            if now <= started + timedelta(hours=CONFIRMATION_TIMEOUT_HOURS):
                continue
        except (ValueError, TypeError, AttributeError):
            # Unknown legacy timestamps must not cause speculative cancellation.
            continue
        deadline=(started+timedelta(hours=CONFIRMATION_TIMEOUT_HOURS)).timestamp()
        from sqlalchemy import inspect
        inspector=inspect(session.bind)
        job_columns=({column['name'] for column in inspector.get_columns('telegram_order_jobs')}
                     if inspector.has_table('telegram_order_jobs') else set())
        clicked=(session.execute(text('''SELECT 1 FROM telegram_order_jobs
            WHERE draft_token=:token AND received_at<=:deadline LIMIT 1'''),
            dict(token=row['token'],deadline=deadline)).scalar()
            if 'draft_token' in job_columns else None)
        if clicked:
            continue
        from telegram_source_identity import source_order_exists
        if source_order_exists(session,row['namespace'],row['chat_id'],row['message_id']):
            continue
        status='flavor_expired' if payload.get('pending_ai_flavor') else 'confirmation_expired'
        result = session.execute(text("UPDATE telegram_order_drafts SET status=:status,waiting_field=NULL,updated_at=:now WHERE token=:token AND status='pending' AND payload=:payload AND updated_at=:previous"),
            dict(status=status,now=now.isoformat(), token=row['token'], payload=row['payload'], previous=row['updated_at']))
        if result.rowcount and 'draft_token' in job_columns:
            session.execute(text("""UPDATE telegram_order_jobs SET state='REJECTED',reason='CONFIRMATION_EXPIRED',
                finished_at=:finished,lease_token=NULL,lease_until=NULL
                WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message
                AND state='NEED_CONFIRMATION'"""),dict(finished=now.timestamp(),namespace=row['namespace'],chat=row['chat_id'],message=row['message_id']))
        count += result.rowcount
    return count


def tick(database):
    with database.SessionLocal() as session:
        expire(session)
        session.commit()
