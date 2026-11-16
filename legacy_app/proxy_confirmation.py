"""Exact-ID, group-scoped proxy confirmation; never changes the bill owner."""
import json
from datetime import datetime,timezone
from sqlalchemy import text
from telegram_source_identity import parameters

# Matched against the read-only 2026-10-16 production identity snapshot.
# Usernames only validate initial provisioning, never authorize a callback.
INITIAL_IDENTITIES={
 '8308154749':'xiayoujie2025','8291204829':'szt7777','8295286580':'laomao_4321',
 '8704420044':'mengmeng_b31','8565649291':'kenanzhengpin'}

def migrate(connection):
    connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS proxy_confirmation_grants (
        telegram_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        granted_by TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS proxy_confirmation_audit (
        draft_token TEXT PRIMARY KEY, actor_id TEXT NOT NULL, owner_id INTEGER NOT NULL REFERENCES users(id),
        chat_id TEXT NOT NULL, order_id INTEGER NOT NULL REFERENCES orders(id), confirmed_at TEXT NOT NULL)''')
    connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS proxy_permission_audit (
        id INTEGER PRIMARY KEY, telegram_id TEXT NOT NULL, enabled INTEGER NOT NULL,
        actor TEXT NOT NULL, changed_at TEXT NOT NULL)''')
    for tid,name in INITIAL_IDENTITIES.items():
        connection.exec_driver_sql('''INSERT OR IGNORE INTO proxy_confirmation_grants
            SELECT telegram_id,1,'user-approved-five-admins',? FROM users
            WHERE telegram_id=? AND lower(username)=?''',
            (datetime.now(timezone.utc).isoformat(),tid,name))

def authorize(session,actor_id,owner,chat_id):
    from models import User
    from telegram_accounts import eligible
    actor_id=str(actor_id)
    grant=session.execute(text('SELECT enabled FROM proxy_confirmation_grants WHERE telegram_id=:id'),dict(id=actor_id)).scalar()
    actor=session.query(User).filter_by(telegram_id=actor_id).first()
    # Proxy grant is independent of customer purchasing authorization. It
    # authorizes only this exact Telegram actor, never their own order/debt.
    active_actor=session.query(User.id).filter(User.telegram_id==actor_id,User.is_active.is_(True)).first()
    if grant!=1 or not active_actor or not eligible(session,owner):raise ValueError('管理员代确认权限无效或已撤销')
    import config
    target=str(getattr(config,'SPLIT_NOTIFICATION_CHAT_ID','') or '')
    binding=session.execute(text('SELECT user_id FROM telegram_account_groups WHERE chat_id=:chat AND active=1'),dict(chat=str(chat_id))).scalar()
    if str(chat_id)!=target and binding!=owner.id:raise ValueError('当前群不允许代确认此客户订单')

def read_draft(session,actor_id,chat_id,token):
    from models import User
    from flavor_draft_expiry import expire
    expire(session,chat_id=str(chat_id))
    row=session.execute(text('SELECT * FROM telegram_order_drafts WHERE namespace=:namespace AND chat_id=:chat AND token=:token'),parameters(chat_id,token=token)).mappings().first()
    if not row or row['status']!='pending' or row['waiting_field']!='confirmation':raise ValueError('订单草稿已失效或不处于待确认状态')
    payload=json.loads(row['payload'])
    if not isinstance(payload.get('pending_order'),dict):raise ValueError('订单确认内容缺失')
    owner=session.get(User,row['user_id']);authorize(session,actor_id,owner,chat_id)
    return dict(row,payload=payload),owner

def validate_submit(session,info,user_id,chat_id,message_id,quote,recipient):
    if not isinstance(info,dict) or set(info)!={'actor_id','draft_token'}:raise ValueError('代确认凭据无效')
    draft,owner=read_draft(session,info['actor_id'],chat_id,info['draft_token'])
    if owner.id!=user_id or draft['message_id']!=message_id:raise ValueError('代确认来源不一致')
    from order_result import monetary_terms
    pending=draft['payload']['pending_order']
    if monetary_terms(pending)!=monetary_terms(quote):raise ValueError('确认报价已变化，请由客户重新确认')
    expected=(pending.get('recipient_name'),pending.get('recipient_phone'),pending.get('recipient_address'))
    if expected!=tuple(recipient):raise ValueError('收件信息已变化，请重新确认')
    return draft

def finish(session,info,draft,order):
    stamp=datetime.now(timezone.utc).isoformat()
    session.execute(text('''INSERT INTO proxy_confirmation_audit
        (draft_token,actor_id,owner_id,chat_id,order_id,confirmed_at)
        VALUES(:token,:actor,:owner,:chat,:order,:at)'''),
        dict(token=draft['token'],actor=str(info['actor_id']),owner=order.user_id,chat=draft['chat_id'],order=order.id,at=stamp))
    changed=session.execute(text("UPDATE telegram_order_drafts SET status='completed',updated_at=:at WHERE token=:token AND status='pending'"),dict(at=stamp,token=draft['token'])).rowcount
    if changed!=1:raise ValueError('草稿已变化，代确认未提交')

def make_admin_router(database,admin_username,security_ready):
    import secrets
    from fastapi import APIRouter,Request,HTTPException
    router=APIRouter()
    def guard(request,write=False):
        if not request.session.get('admin_logged_in'):raise HTTPException(401,'请先登录')
        if request.session.get('admin_username')!=admin_username():raise HTTPException(403,'仅最高管理员可管理代确认权限')
        if not security_ready():raise HTTPException(503,'会话安全配置未就绪')
        if write and not secrets.compare_digest(request.session.get('proxy_csrf','') or 'unset',request.headers.get('X-Proxy-CSRF','')):raise HTTPException(403,'请求校验失败')
    @router.get('/api/proxy-confirmation/grants')
    def grants(request:Request):
        guard(request)
        with database.SessionLocal() as session:
            states=dict(session.execute(text('SELECT telegram_id,enabled FROM proxy_confirmation_grants')).all())
        return dict(csrf=request.session.setdefault('proxy_csrf',secrets.token_urlsafe(32)),
            grants=[dict(telegram_id=tid,username=name,enabled=states.get(tid)==1,identity_verified=tid in states) for tid,name in INITIAL_IDENTITIES.items()])
    @router.post('/api/proxy-confirmation/grants')
    async def change(request:Request):
        guard(request,True)
        try:body=await request.json()
        except ValueError:raise HTTPException(400,'请求格式无效') from None
        if not isinstance(body,dict) or set(body)!={'telegram_id','enabled'} or body.get('telegram_id') not in INITIAL_IDENTITIES or type(body.get('enabled')) is not bool:raise HTTPException(400,'权限目标无效')
        import asyncio
        def commit():
            with database.SessionLocal() as session:
                session.execute(text('BEGIN IMMEDIATE'))
                stamp=datetime.now(timezone.utc).isoformat();params=dict(id=body['telegram_id'],enabled=int(body['enabled']),actor=request.session['admin_username'],at=stamp)
                if session.execute(text('UPDATE proxy_confirmation_grants SET enabled=:enabled,granted_by=:actor,updated_at=:at WHERE telegram_id=:id'),params).rowcount!=1:raise HTTPException(409,'账号身份尚未核实，不能启用')
                session.execute(text('INSERT INTO proxy_permission_audit(telegram_id,enabled,actor,changed_at) VALUES(:id,:enabled,:actor,:at)'),params)
                session.commit();return dict(saved=True)
        return await asyncio.to_thread(commit)
    return router
