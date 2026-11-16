"""Fixed preview snapshots and explicit confirmation; no network calls."""
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import text

def migrate(connection):
    connection.execute(text('''CREATE TABLE IF NOT EXISTS bill_batches (
        operation_key TEXT PRIMARY KEY, operator TEXT NOT NULL, request_hash TEXT NOT NULL,
        cutoff TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)'''))
    connection.execute(text('''CREATE TABLE IF NOT EXISTS bill_batch_retry_audit (
        operation_key TEXT PRIMARY KEY, batch_key TEXT NOT NULL REFERENCES bill_batches(operation_key),
        user_id INTEGER NOT NULL REFERENCES users(id), operator TEXT NOT NULL,
        reason TEXT NOT NULL, prior_error TEXT NOT NULL, created_at TEXT NOT NULL)'''))

def prepare(database, user_ids, operator, operation_key):
    if not isinstance(operation_key,str) or not 16 <= len(operation_key) <= 100:
        raise ValueError('缺少有效批次编号')
    if not operator or not user_ids or len(user_ids)>100:
        raise ValueError('明确选择1至100个用户，禁止默认全库发送')
    if any(type(x) is not int or x<=0 for x in user_ids) or len(set(user_ids))!=len(user_ids):
        raise ValueError('用户名单无效或重复')
    ids=sorted(user_ids)
    fingerprint=hashlib.sha256(json.dumps(ids).encode()).hexdigest()
    from cumulative_bill_snapshot import capture
    from telegram_accounts import notification_target
    from models import User
    with database.SessionLocal() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        old=session.execute(text('SELECT * FROM bill_batches WHERE operation_key=:key'),{'key':operation_key}).mappings().first()
        if old:
            if old['operator']!=operator or old['request_hash']!=fingerprint:
                raise ValueError('批次编号与用户名单或操作人冲突')
            return json.loads(old['payload'])
        cutoff=datetime.now(timezone.utc)
        targets=[]
        for uid in ids:
            user=session.get(User,uid)
            if user is None or not user.is_active:
                raise ValueError('名单包含不存在或停用用户')
            target=notification_target(session,uid)
            if not target or not target.startswith('-'):
                raise ValueError(f'用户 #{uid} 缺少可核对接单群，本批次未登记，不会部分发送')
            facts=capture(session,uid,snapshot_time=cutoff)
            targets.append(dict(user_id=uid,chat_id=target,text=facts['text'],telegram_id=str(user.telegram_id),username=str(user.username or '')))
        result=dict(operation_key=operation_key,cutoff=cutoff.isoformat(),count=len(ids),targets=targets)
        session.execute(text('INSERT INTO bill_batches VALUES(:key,:operator,:hash,:cutoff,\'prepared\',:payload)'),
                        dict(key=operation_key,operator=operator,hash=fingerprint,cutoff=result['cutoff'],payload=json.dumps(result,ensure_ascii=False)))
        session.commit()
        return result

def confirm(database, operator, operation_key, expected_count):
    with database.SessionLocal() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row=session.execute(text('SELECT * FROM bill_batches WHERE operation_key=:key'),{'key':operation_key}).mappings().first()
        if not row or row['operator']!=operator:
            raise ValueError('批次不存在或操作人不匹配')
        facts=json.loads(row['payload'])
        if type(expected_count) is not int or facts['count']!=expected_count:
            raise ValueError('确认人数与固定名单不一致')
        duplicate=row['state']=='queued'
        if not duplicate:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(facts['cutoff'])).total_seconds()
            if age<0 or age>300:
                raise ValueError('账单预览已超过5分钟，请重新生成批次')
            from telegram_accounts import notification_target
            from models import User
            for target in facts['targets']:
                user=session.get(User,target['user_id'])
                if user is None or not user.is_active:
                    raise ValueError('批次用户已停用，禁止发送')
                if target.get('telegram_id')!=str(user.telegram_id) or target.get('username')!=str(user.username or ''):
                    raise ValueError('账单所属Telegram账号或用户名已变化，禁止发送旧快照，请重新预览')
                if notification_target(session,target['user_id'])!=target['chat_id']:
                    raise ValueError('接单群归属已变化，禁止发送旧批次')
                session.execute(text('''INSERT INTO telegram_notification_events
                    (event_key,user_id,chat_id,kind,payload,status,created_at)
                    VALUES(:key,:user,:chat,'batch_bill',:payload,'pending',:time)'''),
                    dict(key='bill-batch:'+operation_key+':'+str(target['user_id']),user=target['user_id'],
                         chat=target['chat_id'],payload=json.dumps({'text':target['text']},ensure_ascii=False),time=facts['cutoff']))
            session.execute(text("UPDATE bill_batches SET state='queued' WHERE operation_key=:key"),{'key':operation_key})
        session.commit()
        return dict(count=facts['count'],duplicate=duplicate,state='queued',business_writes=0)

def progress(database, operator, operation_key):
    with database.SessionLocal() as session:
        row=session.execute(text('SELECT operator,state,payload FROM bill_batches WHERE operation_key=:key'),{'key':operation_key}).mappings().first()
        if not row or row['operator']!=operator:
            raise ValueError('批次不存在或操作人不匹配')
        facts=json.loads(row['payload']);counts={}
        targets=[]
        for target in facts['targets']:
            event=session.execute(text('SELECT status,last_error FROM telegram_notification_events WHERE event_key=:key'),
                    {'key':'bill-batch:'+operation_key+':'+str(target['user_id'])}).mappings().first()
            status=event['status'] if event else 'prepared'
            counts[status]=counts.get(status,0)+1
            targets.append(dict(user_id=target['user_id'],status=status,error=event['last_error'] if event else None))
        return dict(count=facts['count'],cutoff=facts['cutoff'],state=row['state'],counts=counts,targets=targets)

def retry(database, operator, batch_key, user_id, reason, operation_key):
    reason=str(reason or '').strip()
    if not reason or len(reason)>500 or not isinstance(operation_key,str) or not 16<=len(operation_key)<=100:
        raise ValueError('恢复缘由和有效操作编号必填')
    allowed={'telegram_http_400','telegram_http_401','telegram_http_403','telegram_http_404','telegram_http_422',
             'telegram_delivery_queue_full','telegram_retry_after_exhausted','telegram_reply_unavailable',
             'BOT token missing','chat_id missing'}
    with database.SessionLocal() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        old=session.execute(text('SELECT * FROM bill_batch_retry_audit WHERE operation_key=:key'),{'key':operation_key}).mappings().first()
        if old:
            if (old['batch_key'],old['user_id'],old['operator'],old['reason'])!=(batch_key,user_id,operator,reason):
                raise ValueError('恢复编号内容冲突')
            return dict(duplicate=True,business_writes=0)
        batch=session.execute(text('SELECT operator FROM bill_batches WHERE operation_key=:key'),{'key':batch_key}).scalar()
        if batch!=operator:raise ValueError('批次操作人不匹配')
        key='bill-batch:'+batch_key+':'+str(user_id)
        event=session.execute(text('SELECT * FROM telegram_notification_events WHERE event_key=:key'),{'key':key}).mappings().first()
        if not event or event['kind']!='batch_bill' or event['status']!='failed' or event['last_error'] not in allowed:
            raise ValueError('仅允许恢复已证明未送达的通知，未知结果不得重发')
        from telegram_accounts import notification_target
        if notification_target(session,user_id)!=event['chat_id']:
            raise ValueError('接单群归属已变化，禁止恢复旧通知')
        from models import User
        user=session.get(User,user_id)
        stored=session.execute(text('SELECT payload FROM bill_batches WHERE operation_key=:key'),{'key':batch_key}).scalar()
        owner=next((t for t in json.loads(stored).get('targets',[]) if t.get('user_id')==user_id),{}) if stored else {}
        if user is None or not user.is_active or owner.get('telegram_id')!=str(user.telegram_id) or owner.get('username')!=str(user.username or ''):
            raise ValueError('账单所属账号已变化或历史身份资料不足，禁止恢复旧快照')
        session.execute(text('INSERT INTO bill_batch_retry_audit VALUES(:key,:batch,:user,:operator,:reason,:error,:at)'),
            dict(key=operation_key,batch=batch_key,user=user_id,operator=operator,reason=reason,error=event['last_error'],at=datetime.now(timezone.utc).isoformat()))
        session.execute(text("UPDATE telegram_notification_events SET status='pending',last_error=NULL WHERE event_key=:key"),{'key':key})
        session.commit()
        return dict(duplicate=False,business_writes=0)

async def drain_one(database, send):
    """One batch event per worker tick; yield entirely to ordinary notices."""
    with database.SessionLocal() as session:
        from telegram_source_identity import account_cutover
        boundary=account_cutover()
        values={'cutover':boundary.isoformat() if boundary else None}
        if session.execute(text("SELECT 1 FROM telegram_notification_events WHERE status='pending' AND kind!='batch_bill' AND (:cutover IS NULL OR julianday(created_at)>=julianday(:cutover)) LIMIT 1"),values).scalar():
            return dict(attempted=False,detail='ordinary_notice_priority')
        event=session.execute(text("SELECT event_key,user_id,payload FROM telegram_notification_events WHERE status='pending' AND kind='batch_bill' AND (:cutover IS NULL OR julianday(created_at)>=julianday(:cutover)) ORDER BY created_at,event_key LIMIT 1"),values).mappings().first()
        if not event:return dict(attempted=False,detail='empty')
        from models import User
        user=session.get(User,event['user_id'])
        batch_key=event['event_key'].partition(':')[2].rpartition(':')[0]
        row=session.execute(text('SELECT payload FROM bill_batches WHERE operation_key=:key'),{'key':batch_key}).scalar()
        facts=json.loads(row) if row else {}
        owner=next((t for t in facts.get('targets',[]) if t.get('user_id')==event['user_id']),{})
        if user is None or not user.is_active or owner.get('telegram_id')!=str(user.telegram_id) or owner.get('username')!=str(user.username or ''):
            session.execute(text("UPDATE telegram_notification_events SET status='failed',last_error='identity_changed_or_unverifiable_do_not_retry' WHERE event_key=:key AND status='pending'"),{'key':event['event_key']})
            session.commit()
            return dict(attempted=False,detail='identity_changed')
        from telegram_accounts import notification_target
        target=session.execute(text('SELECT chat_id FROM telegram_notification_events WHERE event_key=:key'),{'key':event['event_key']}).scalar()
        if notification_target(session,event['user_id'])!=target:
            session.execute(text("UPDATE telegram_notification_events SET status='failed',last_error='target_changed_do_not_retry' WHERE event_key=:key AND status='pending'"),{'key':event['event_key']})
            session.commit()
            return dict(attempted=False,detail='target_changed')
        event=dict(event)
    from account_notifications import dispatch
    return await dispatch(database,event['event_key'],event['user_id'],'batch_bill',json.loads(event['payload'])['text'],send)
