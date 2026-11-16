"""Single-source recovery preview + explicit audit; never submits an order."""
import hashlib
import hmac
import json
import time
from datetime import datetime,timezone
from sqlalchemy import text

def migrate(connection):
    connection.execute(text('''CREATE TABLE IF NOT EXISTS telegram_queue_recovery_audit (
        operation_key TEXT PRIMARY KEY, namespace TEXT NOT NULL, chat_id TEXT NOT NULL,
        message_id INTEGER NOT NULL, old_state TEXT NOT NULL, new_state TEXT NOT NULL,
        operator TEXT NOT NULL, reason TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL, result TEXT NOT NULL)'''))

def read_job(session,namespace,chat_id,message_id):
    row=session.execute(text('SELECT * FROM telegram_order_jobs WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message'),
        dict(namespace=namespace,chat=str(chat_id),message=message_id)).mappings().first()
    if row is None:raise ValueError('来源任务不存在')
    return dict(row)

def version(row):
    return hashlib.sha256(json.dumps([row[k] for k in ('namespace','chat_id','message_id','state','attempts','fingerprint','lease_token')]).encode()).hexdigest()

def decide(session,row):
    if row['state'] not in ('REVIEW_REQUIRED','FAILED_RETRYABLE'):
        return 'BLOCKED','当前状态不支持人工恢复'
    if row['lease_token']:
        return 'BLOCKED','任务仍有处理租约'
    if row['message_id']<0:
        return 'BLOCKED','按钮涉及草稿版本依赖，请使用当前草稿按钮，不重放旧点击'
    if row['reason']=='BOT_ACCOUNT_CUTOVER_HISTORICAL_UPDATE':
        return 'BLOCKED','消息早于换账号，保留核查，不自动重放'
    from telegram_source_identity import source_order_exists,account_cutover
    order=source_order_exists(session,row['namespace'],row['chat_id'],row['message_id'])
    if order:return 'COMPLETED','已有订单事实，仅核对任务状态，不再次录单'
    from telegram_source_identity import scope
    if row['namespace'] not in ('shared',scope(1)):
        return 'BLOCKED','来源属于旧机器人或归属未知，禁止恢复处理'
    boundary=account_cutover()
    if boundary is not None and row['received_at']<boundary.timestamp():
        return 'BLOCKED','来源早于本次换账号，保留历史记录，不自动恢复旧消息'
    if not row['payload']:return 'BLOCKED','来源已缺失，无法安全恢复'
    linked=session.execute(text('''SELECT d.payload,d.status,d.message_id FROM telegram_message_events e
        JOIN telegram_order_drafts d ON d.token=e.draft_token AND d.namespace=e.namespace
        WHERE e.namespace=:namespace AND e.chat_id=:chat AND e.message_id=:message
        AND e.sender_id=:sender AND d.sender_id=:sender AND d.user_id=e.user_id'''),
        dict(namespace=row['namespace'],chat=row['chat_id'],message=row['message_id'],sender=row['sender_id'])).mappings().first()
    if linked:
        if linked['status']!='pending':return 'BLOCKED','原草稿已关闭或过期，禁止恢复旧来源'
        if linked['message_id']==row['message_id']:
            data=json.loads(linked['payload'])
            prompts=data.get('_reply_message_ids')
            if isinstance(prompts,list) and any(type(n) is int and n>0 for n in prompts):
                for key in ('pending_order','pending_ai_flavor','pending_recipient_order','pending_manual_review'):
                    value=data.get(key)
                    ready=isinstance(value,dict) and (
                        key=='pending_order' and bool(value.get('items'))
                        or key=='pending_recipient_order' and isinstance(value.get('order_info'),dict) and bool(value['order_info'].get('items'))
                        or key=='pending_ai_flavor' and isinstance(value.get('result'),dict) and isinstance(value.get('proposal'),dict) and bool(value['proposal'].get('choices'))
                        or key=='pending_manual_review' and bool(value.get('raw_text')) and isinstance(value.get('reasons'),list))
                    if ready:
                        return 'NEED_CONFIRMATION','已有待确认草稿及回执，仅修复任务状态，不重复识别或通知'
    return 'RECEIVED','重新排入该来源，工作者必须再次核对订单/草稿事实；不在后台直接提交订单'

def validate_source(row,secret,session):
    import base64
    from cryptography.fernet import Fernet,InvalidToken
    key=base64.urlsafe_b64encode(hashlib.sha256(('rainbow-order-replay-v1:'+secret).encode()).digest())
    try:
        raw=Fernet(key).decrypt(row['payload'].encode())
        if hashlib.sha256(raw).hexdigest()!=row['fingerprint']:raise ValueError()
        message=json.loads(raw)['message']
        if (str(message['chat']['id']),int(message['message_id']),str(message['from']['id'])) != (
            row['chat_id'],row['message_id'],row['sender_id']):raise ValueError()
        if not isinstance(message.get('text'),str) or not message['text'].strip():raise ValueError()
    except (InvalidToken,KeyError,TypeError,ValueError,AttributeError):
        raise ValueError('来源内容或身份校验失败，禁止重新排入队列') from None
    from sqlalchemy import inspect
    if row['namespace']!='shared' and inspect(session.connection()).has_table('telegram_message_observers'):
        activated=session.execute(text('SELECT activated_at FROM telegram_message_observers WHERE bot_id=:bot'),
            dict(bot=row['namespace'].partition(':')[2])).scalar()
        if activated:
            stamp=message.get('date')
            import math
            if type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<datetime.fromisoformat(activated).timestamp():
                raise ValueError('来源早于当前机器人启用时间，禁止恢复旧消息')

def preview(database,secret,namespace,chat_id,message_id,now=None):
    now=int(time.time() if now is None else now)
    with database.SessionLocal() as session:
        row=read_job(session,namespace,chat_id,message_id)
        action,explanation=decide(session,row)
        if action=='RECEIVED':validate_source(row,secret,session)
    value=version(row);body='%s:%s:%s' % (value,action,now)
    token=hmac.new(secret.encode(),body.encode(),hashlib.sha256).hexdigest()
    return dict(action=action,explanation=explanation,version=value,issued_at=now,token=token,
        namespace=namespace,chat_id=str(chat_id),message_id=message_id,state=row['state'],attempts=row['attempts'])

def apply(database,secret,request,operator,reason,operation_key,now=None):
    now=int(time.time() if now is None else now)
    if not operator or not str(reason).strip() or len(str(reason))>500:raise ValueError('恢复理由必填，最多500字')
    if not isinstance(operation_key,str) or not 16<=len(operation_key)<=100:raise ValueError('操作编号无效')
    if not 0<=now-int(request['issued_at'])<=300:raise ValueError('核查预览已过期，请重新核查')
    body='%s:%s:%s' % (request['version'],request['action'],request['issued_at'])
    signature=hmac.new(secret.encode(),body.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature,str(request['token'])):raise ValueError('核查凭据无效')
    with database.SessionLocal() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        prior=session.execute(text('SELECT * FROM telegram_queue_recovery_audit WHERE operation_key=:key'),dict(key=operation_key)).mappings().first()
        if prior:
            if (prior['namespace'],prior['chat_id'],prior['message_id'],prior['operator'],prior['reason']) != (
                request['namespace'],str(request['chat_id']),request['message_id'],operator,reason):
                raise ValueError('操作编号已用于其他恢复请求')
            return json.loads(prior['result'])
        row=read_job(session,request['namespace'],request['chat_id'],request['message_id'])
        if version(row)!=request['version']:raise ValueError('任务已变化，请重新核查')
        action,_=decide(session,row)
        if action=='BLOCKED' or action!=request['action']:raise ValueError('关联事实已变化或禁止恢复，请重新核查')
        if action=='RECEIVED':validate_source(row,secret,session)
        result=dict(state=action,source=row['fingerprint'],business_posts=0)
        session.execute(text('''UPDATE telegram_order_jobs SET state=:state,
            reason='ADMIN_SOURCE_FACT_RECOVERY',lease_until=NULL,lease_token=NULL,
            attempts=CASE WHEN :state='RECEIVED' THEN 0 ELSE attempts END,
            finished_at=CASE WHEN :state='RECEIVED' THEN NULL ELSE :now END
            WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message'''),
            dict(state=action,now=now,namespace=row['namespace'],chat=row['chat_id'],message=row['message_id']))
        session.execute(text('''INSERT INTO telegram_queue_recovery_audit
            VALUES(:key,:namespace,:chat,:message,:old,:new,:operator,:reason,:fingerprint,:created,:result)'''),
            dict(key=operation_key,namespace=row['namespace'],chat=row['chat_id'],message=row['message_id'],old=row['state'],
                new=action,operator=operator,reason=reason,fingerprint=row['fingerprint'],
                created=datetime.now(timezone.utc).isoformat(),result=json.dumps(result)))
        session.commit()
        return result
