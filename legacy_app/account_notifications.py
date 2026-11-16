"""Post-commit Telegram notices, never a second ledger or business retry."""
import json
import html
import logging
import re
from datetime import datetime,timezone
from sqlalchemy import text
import telegram_accounts as accounts

logger=logging.getLogger(__name__)


def bill_owner_mention(user):
    """Account identity only, never recipient/contact/recent-speaker inference."""
    telegram_id=str(getattr(user,'telegram_id','') or '')
    if not telegram_id.isdigit() or int(telegram_id)<=0:
        raise ValueError('账单所属用户缺少有效Telegram身份')
    username=str(getattr(user,'username','') or '').lstrip('@')
    if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',username):
        # Display @username, but pin the actual mention to immutable Telegram ID.
        return '<a href="tg://user?id='+telegram_id+'">@'+html.escape(username)+'</a>'
    label=str(getattr(user,'telegram_name',None) or getattr(user,'display_name',None) or '账单所属用户')
    return '<a href="tg://user?id='+telegram_id+'">'+html.escape(label)+'</a>'


def prepare_bill_payload(session,user_id,kind,payload):
    value=dict(payload)
    if kind not in {'payment','manual_bill'} or not value.get('text','').startswith('🧾 账单'):
        return value
    from models import User
    user=session.get(User,user_id)
    if user is None:raise ValueError('账单所属用户不存在')
    try:mention=bill_owner_mention(user)
    except ValueError:
        # Notification decoration cannot invalidate a committed/payment ledger.
        logger.warning('BILL_MENTION_UNAVAILABLE account=%s reason=invalid_telegram_identity',user_id)
        return value
    value['text']=mention+'\n\n'+value['text']
    value['mention_user_id']=str(user.telegram_id)
    return value


def bill_text(summary,received=None):
    lines=['🧾 账单更新' if received is not None else '🧾 账单','']
    if received is not None:lines.append(f'本次收款：¥{float(received):.2f}')
    lines.extend([f"今日订单：{summary['today_count']}单 / ¥{summary['today_amount']:.2f}",
                  f"已结货款：¥{summary['settled']:.2f}",
                  f"待结货款：¥{summary['pending']:.2f}"])
    return '\n'.join(lines)


def payment_text(result):
    if result.get('cumulative_bill_snapshot') is not None:
        return result['cumulative_bill_snapshot']['text']
    if result.get('bill_snapshot') is not None:
        return bill_text(result['bill_snapshot'],result['amount'])
    lines=['💰 收款成功',f"本次入账：¥{float(result.get('amount') or 0):.2f}"]
    settled=sum(float(row.get('paid') or 0) for row in result.get('affected_orders',[]))
    if settled:lines.append(f'本次冲减待结账：¥{settled:.2f}')
    prepaid=float(result.get('prepaid_added') or 0)
    if prepaid:lines.append(f'转入预存：¥{prepaid:.2f}')
    lines.extend([f"当前待结账：¥{float(result.get('user_debt') or 0):.2f}",
                  f"当前预存：¥{float(result.get('user_balance') or 0):.2f}"])
    return '\n'.join(lines)


async def dispatch(database,key,user_id,kind,message,send):
    from cancel_notice_hold import guard_event
    guard_event(key, 'dispatch')
    # Always save the event, even when deployment admission is paused.
    with database.SessionLocal() as session:
        key=accounts.payment_notice_key(session,key,user_id,kind)
        from cancel_notice_hold import guard_event
        guard_event(key, 'notice_claim')
        accounts.queue_notice(session,key,user_id,kind,{'text':message});session.commit()
    from deployment_drain import begin_activity,end_activity
    activity=begin_activity()
    if activity is None:return dict(attempted=False,sent=False,detail='deployment_paused',sent_count=0,failed_count=0)
    try:return await _dispatch_active(database,key,user_id,kind,message,send)
    finally:end_activity(activity)


async def _dispatch_active(database,key,user_id,kind,message,send):
    """Persist a unique event and claim before sending, including across restarts.

    A crash/timeout after dispatch is uncertain, never blindly replayed. HTTP
    429 retry is exclusively inside the existing network sender, not this layer.
    """
    with database.SessionLocal() as session:
        key=accounts.payment_notice_key(session,key,user_id,kind)
        from cancel_notice_hold import guard_event
        guard_event(key, 'notice_claim')
        accounts.queue_notice(session,key,user_id,kind,{'text':message})
        session.commit()
        claimed=session.execute(text("UPDATE telegram_notification_events SET status='sending',attempts=attempts+1 WHERE event_key=:key AND status='pending'"),dict(key=key)).rowcount
        session.commit()
        if not claimed:return dict(attempted=False,sent=False,detail='already_dispatched',sent_count=0,failed_count=0)
        event=session.execute(text('SELECT chat_id,payload FROM telegram_notification_events WHERE event_key=:key'),dict(key=key)).mappings().one()
        target,payload=event['chat_id'],json.loads(event['payload'])
        # Existing committed payment outbox rows also get the same owner header.
        if not payload.get('mention_user_id'):
            enriched=prepare_bill_payload(session,user_id,kind,payload)
            if enriched!=payload:
                payload=enriched
                session.execute(text('UPDATE telegram_notification_events SET payload=:payload WHERE event_key=:key'),dict(key=key,payload=json.dumps(payload,ensure_ascii=False)))
                session.commit()
    from notice_observation import begin,end,observe
    observation_token=begin(key)
    try:
        reply_id=payload.get('reply_to_message_id')
        kwargs={'parse_mode':'HTML'}
        if reply_id is not None:kwargs['reply_to_message_id']=reply_id
        ok,detail=await send(target,payload['text'],**kwargs)
        fallback=payload.get('reply_fallback')
        # Retry without Reply only after Telegram definitively rejected the
        # nonexistent source. A timeout is uncertain and must never resend.
        if not ok and reply_id is not None and detail=='telegram_reply_unavailable':
            fallback='source_message_unavailable'
            logger.warning('CANCEL_REPLY_FALLBACK event=%s reason=%s',key,fallback)
            ok,detail=await send(target,payload['text'],parse_mode='HTML')
        elif fallback:
            logger.info('CANCEL_REPLY_FALLBACK event=%s reason=%s',key,fallback)
        # Persist only controlled error categories, never provider URLs/secrets.
        # Only a controlled, definitive rejection proves that no message was
        # accepted. Transport/response failures and HTTP 5xx remain uncertain.
        definitive = bool(re.fullmatch(r'telegram_http_(?:400|401|403|404|422)', str(detail))) or detail in {
            'telegram_delivery_queue_full', 'telegram_retry_after_exhausted', 'telegram_reply_unavailable',
            'BOT token missing', 'chat_id missing'}
        error=None if ok else str(detail) if definitive else 'delivery_unknown'
        observe('DISPATCH_RESULT',detail='ok' if ok else detail)
    except Exception as exc:
        observe('DISPATCH_EXCEPTION',error=exc)
        ok=False;error='delivery_unknown'
    finally:
        end(observation_token)
    with database.SessionLocal() as session:
        session.execute(text('UPDATE telegram_notification_events SET status=:status,last_error=:error,sent_at=:time WHERE event_key=:key'),
            dict(key=key,status='sent' if ok else 'unknown' if error=='delivery_unknown' else 'failed',error=fallback if ok and fallback else error,time=datetime.now(timezone.utc).isoformat() if ok else None))
        if ok:
            session.execute(text('UPDATE telegram_account_groups SET last_notice_at=:time WHERE chat_id=:chat'),dict(chat=target,time=datetime.now(timezone.utc).isoformat()))
        session.commit()
    return dict(attempted=True,sent=ok,detail='ok' if ok else error,sent_count=int(ok),failed_count=int(not ok))


async def order_status_notice(database,session,order,status,message,send):
    # Cancellation is delivered exclusively from the transaction's exact-reason
    # outbox event, never a second generic status notice/manual callback.
    return False,'suppressed_cancel_outbox_only' if status=='cancelled' else 'suppressed_non_payment_event'


async def drain_pending(database,send,limit=5):
    """Recover committed, never-attempted events only; uncertain sends stay frozen."""
    with database.SessionLocal() as session:
        from telegram_source_identity import account_cutover
        boundary=account_cutover()
        rows=session.execute(text("SELECT event_key,user_id,kind,payload FROM telegram_notification_events WHERE status='pending' AND kind!='batch_bill' AND (:cutover IS NULL OR julianday(created_at)>=julianday(:cutover)) ORDER BY created_at,event_key LIMIT :limit"),
                             dict(limit=max(1,min(int(limit),5))+1,cutover=boundary.isoformat() if boundary else None)).mappings().all()
        from cancel_notice_hold import held, guard_event, CancellationReplayHeld
        events=[]
        for row in rows:
            if held(row['event_key']):
                try:guard_event(row['event_key'], 'pending_recovery')
                except CancellationReplayHeld:continue
            events.append(dict(row))
            if len(events)>=max(1,min(int(limit),5)):break
    for event in events:
        if event['kind'] in {'pending','confirmed','shipped','processing','recorded'}:
            with database.SessionLocal() as session:
                session.execute(text("UPDATE telegram_notification_events SET status='suppressed' WHERE event_key=:key AND status='pending'"),dict(key=event['event_key']))
                session.commit()
            continue
        await dispatch(database,event['event_key'],event['user_id'],event['kind'],
                       json.loads(event['payload'])['text'],send)
    return len(events)
