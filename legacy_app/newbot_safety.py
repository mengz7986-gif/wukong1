"""Opt-in new-server ingestion gates. No business writes, migrations or polling."""
from datetime import datetime,timezone
import fcntl,json,math,os,time
from pathlib import Path

def policy():
    path=os.getenv('NEWBOT_INGESTION_POLICY_PATH','')
    if not path:return None  # Other deployments retain their existing behavior.
    try:
        p=Path(path)
        if p.is_symlink():raise ValueError()
        data=json.loads(p.read_text())
        if data.get('version')!=1 or type(data.get('enabled')) is not bool:raise ValueError()
        if not str(data.get('bot_id','')).isdigit() or not isinstance(data.get('username'),str):raise ValueError()
        groups=data.get('allowed_groups')
        if not isinstance(groups,list) or len(groups)!=len(set(groups)):raise ValueError()
        if any(type(g) is not int or g>=0 for g in groups):raise ValueError()
        if data.get('authorization_mode','group_allowlist') not in ('group_allowlist','sender_authorized'):raise ValueError()
        token=os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN','')
        if str(data['bot_id'])!=token.partition(':')[0]:raise ValueError()
        boundary=data.get('activated_at')
        if boundary is not None:
            stamp=datetime.fromisoformat(boundary.replace('Z','+00:00'))
            if stamp.tzinfo is None:raise ValueError()
            data['_boundary']=stamp.astimezone(timezone.utc)
        else:data['_boundary']=None
        return data
    except Exception:raise ValueError('NEWBOT_POLICY_INVALID') from None

def require_ready(data=None):
    data=policy() if data is None else data
    if data is None:return None
    if not data['enabled']:raise ValueError('NEWBOT_INGESTION_DISABLED')
    if data.get('authorization_mode','group_allowlist')=='group_allowlist' and not data['allowed_groups']:raise ValueError('NEWBOT_GROUP_LIST_REQUIRED')
    if data['_boundary'] is None:raise ValueError('NEWBOT_ACTIVATION_REQUIRED')
    if data['_boundary'].timestamp()>time.time():raise ValueError('NEWBOT_ACTIVATION_NOT_REACHED')
    return data

def allowed(chat_id,stamp):
    try:
        data=require_ready()
        if data is None:return True
        if type(stamp) not in (int,float) or not math.isfinite(stamp):return False
        if stamp<=data['_boundary'].timestamp():return False
        chat=int(chat_id)
        # This gate validates source time only in sender-authorized mode.
        # Existing bot/API account authorization remains mandatory; this
        # does not authorize a user merely because they share a group.
        return chat!=0 and (data.get('authorization_mode')=='sender_authorized' or chat>0 or chat in data['allowed_groups'])
    except Exception:return False

def payload_allowed(chat_id,payload):
    if not os.getenv('NEWBOT_INGESTION_POLICY_PATH'):return True
    origin=payload.get('message') or (payload.get('callback_query') or {}).get('message') or {}
    actual=(origin.get('chat') or {}).get('id')
    if actual is None or str(actual)!=str(chat_id):return False
    return allowed(chat_id,origin.get('date'))

def update_allowed(update):
    if not os.getenv('NEWBOT_INGESTION_POLICY_PATH'):return True
    message=getattr(update,'effective_message',None)
    chat=getattr(update,'effective_chat',None)
    stamp=getattr(message,'date',None)
    return allowed(getattr(chat,'id',0),stamp.timestamp() if isinstance(stamp,datetime) else None)

def validate_submit(session,chat_id,message_id):
    if not os.getenv('NEWBOT_INGESTION_POLICY_PATH') or chat_id is None:return
    from sqlalchemy import text
    from telegram_source_identity import scope
    require_ready()
    stamp=session.execute(text('''SELECT source_date FROM telegram_order_jobs
        WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message'''),
        dict(namespace=scope(chat_id),chat=str(chat_id),message=message_id)).scalar()
    if not allowed(chat_id,stamp):raise ValueError('NEWBOT_SOURCE_NOT_ALLOWED')

def startup_guard():
    data=require_ready()
    if data is None:return None
    lock=Path(os.getenv('NEWBOT_SINGLE_INSTANCE_LOCK','/var/lib/newbot/ingestion/bot.lock'))
    lock.parent.mkdir(parents=True,exist_ok=True)
    handle=lock.open('a')
    try:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        import requests
        token=os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN','')
        result=requests.post('https://api.telegram.org/bot'+token+'/getMe',timeout=15).json()
        user=result.get('result',{})
        if not result.get('ok') or str(user.get('id'))!=str(data['bot_id']) or user.get('username','').casefold()!=data['username'].lstrip('@').casefold():raise ValueError()
        webhook=requests.post('https://api.telegram.org/bot'+token+'/getWebhookInfo',timeout=15).json()
        if not webhook.get('ok') or webhook.get('result',{}).get('url'):raise ValueError()
        if data.get('authorization_mode')=='sender_authorized' and not user.get('can_read_all_group_messages'):raise ValueError()
        for chat in ([] if data.get('authorization_mode')=='sender_authorized' else data['allowed_groups']):
            membership=requests.post('https://api.telegram.org/bot'+token+'/getChatMember',
                json={'chat_id':chat,'user_id':int(data['bot_id'])},timeout=15).json()
            room=requests.post('https://api.telegram.org/bot'+token+'/getChat',json={'chat_id':chat},timeout=15).json()
            member=membership.get('result',{});info=room.get('result',{})
            if not membership.get('ok') or not room.get('ok') or info.get('id')!=chat or info.get('type') not in ('group','supergroup'):raise ValueError()
            admin=member.get('status') in ('creator','administrator')
            if not admin and member.get('status') not in ('member','restricted'):raise ValueError()
            if not admin and not user.get('can_read_all_group_messages'):raise ValueError()
            if member.get('status')=='restricted' and (not member.get('is_member') or not member.get('can_send_messages')):raise ValueError()
            if not admin and info.get('permissions',{}).get('can_send_messages') is False:raise ValueError()
        return handle
    except Exception:
        handle.close();raise ValueError('NEWBOT_STARTUP_IDENTITY_OR_INSTANCE_CHECK_FAILED') from None
