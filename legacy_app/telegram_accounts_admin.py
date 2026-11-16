"""Existing admin/API boundary for group identity and unified account records."""
import json
import hashlib
import re
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Form, HTTPException,Request
from sqlalchemy import text
from models import User
import telegram_accounts as accounts
from telegram_source_identity import parameters


def make_router(session_dependency,auth_dependency):
    router=APIRouter(dependencies=[Depends(auth_dependency)])

    @router.get('/api/telegram/account')
    def identity(telegram_id: str='',chat_id: str='',session=Depends(session_dependency)):
        if chat_id:
            user,binding=accounts.bound_account(session,chat_id)
        else:
            user=session.query(User).filter_by(telegram_id=str(telegram_id)).first();binding=None
        return dict(bound=bool(binding),authorized=accounts.eligible(session,user),
            user_id=user.id if user else None,telegram_id=user.telegram_id if user else None,
            username=user.username if user else None,binding=binding)

    @router.get('/api/users/{user_id}/telegram-group')
    def group(user_id:int,session=Depends(session_dependency)):
        return [dict(row) for row in session.execute(text('SELECT * FROM telegram_account_groups WHERE user_id=:user ORDER BY bound_at DESC'),dict(user=user_id)).mappings()]

    @router.post('/api/users/{user_id}/telegram-group')
    def change(user_id:int,request:Request,chat_id:str=Form(...),title:str=Form(''),action:str=Form('bind'),session=Depends(session_dependency)):
        actor=str(request.session.get('admin_username') or 'authenticated-api')
        def snapshot():
            return [dict(r) for r in session.execute(text(
                'SELECT chat_id,user_id,title,active FROM telegram_account_groups WHERE user_id=:user ORDER BY chat_id'),dict(user=user_id)).mappings()]
        def audit(before,after,result):
            # Dedicated DB audit only: no group titles, identifiers or credentials
            # in ordinary logs. Same commit as the successful binding mutation.
            fact=dict(actor=actor,action=action,target_user_id=user_id,target_chat_id=chat_id,
                      before=before,after=after,result=result)
            encoded=json.dumps(fact,ensure_ascii=False,sort_keys=True)
            session.execute(text('''INSERT INTO telegram_business_operations
                (operation_key,user_id,kind,request_hash,result,created_at)
                VALUES(:key,:user,'ADMIN_GROUP_BINDING_AUDIT',:hash,:result,:at)'''),
                dict(key='admin-group-audit:'+uuid.uuid4().hex,user=user_id,
                     hash=hashlib.sha256(encoded.encode()).hexdigest(),result=encoded,
                     at=datetime.now(timezone.utc).isoformat()))
        before=[]
        try:
            if session.bind.dialect.name=='sqlite' and not session.connection().connection.driver_connection.in_transaction:
                session.execute(text('BEGIN IMMEDIATE'))
            if session.get(User,user_id) is None:raise ValueError('客户不存在')
            before=snapshot()
            if action=='unbind':accounts.unbind(session,user_id,chat_id);result=dict(unbound=True)
            elif action in ('bind','replace'):result=accounts.bind(session,user_id,chat_id,title,actor,replace=action=='replace')
            else:raise ValueError('操作无效')
            after=snapshot()
            audit(before,after,'SUCCESS' if before!=after else 'NO_CHANGE')
            session.commit();return result
        except (ValueError,TypeError) as error:
            session.rollback()
            if session.get(User,user_id) is not None:
                audit(before,snapshot(),'REJECTED');session.commit()
            raise HTTPException(400,str(error)) from None
        except Exception:
            session.rollback();raise

    @router.post('/api/users/{user_id}/telegram-group/probe')
    def probe_group(user_id:int,session=Depends(session_dependency)):
        user=session.get(User,user_id)
        row=session.execute(text('SELECT chat_id FROM telegram_account_groups WHERE user_id=:user AND active=1'),dict(user=user_id)).first()
        if not user or not row:raise HTTPException(404,'客户未绑定有效接单群')
        from telegram_group_health import probe
        from config import BOT_TOKEN
        result=probe(row[0],BOT_TOKEN)
        session.execute(text('UPDATE telegram_account_groups SET bot_status=:status WHERE chat_id=:chat AND user_id=:user'),
                        dict(status=result['status'],chat=row[0],user=user_id))
        session.commit()
        return result

    @router.get('/api/telegram/account/summary')
    def summary(telegram_id:str,session=Depends(session_dependency)):
        user=session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:raise HTTPException(404,'客户不存在')
        return accounts.summary(session,user.id)

    @router.get('/api/telegram/account/orders')
    def search(telegram_id:str,keyword:str='',offset:int=0,session=Depends(session_dependency)):
        user=session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:raise HTTPException(404,'客户不存在')
        try:rows=accounts.search_orders(session,user.id,keyword,offset)
        except ValueError as error:raise HTTPException(400,str(error)) from None
        __import__('product_units').prefetch(session,[item for order in rows for item in order.items])
        return [dict(id=o.id,order_number=o.order_number,created_at=o.created_at.isoformat(),
            recipient_name=o.recipient_name,recipient_phone=o.recipient_phone,recipient_address=o.recipient_address,
            grand_total=o.grand_total,total_shipping_fee=o.total_shipping_fee,status=o.status,payment_status=o.payment_status,
            waybill=o.fhd_waybill_code,carrier=o.fhd_cp_name,
            items=[dict(product_name=i.product.name,parent_name=i.product.parent.name if i.product.parent else '',quantity=i.quantity,unit=__import__('product_units').historical(session,i),unit_price=i.unit_price) for i in o.items]) for o in rows]

    @router.post('/api/telegram/message/claim')
    def claim(chat_id:str=Form(...),message_id:int=Form(...),sender_id:str=Form(...),session=Depends(session_dependency)):
        user=session.query(User).filter_by(telegram_id=str(sender_id)).first()
        if not accounts.eligible(session,user):raise HTTPException(403,'下单资格未通过')
        claimed=accounts.claim_message(session,chat_id,message_id,sender_id,user.id)
        session.commit();return dict(claimed=claimed)

    @router.post('/api/telegram/draft/open')
    def draft_open(chat_id:str=Form(...),message_id:int=Form(...),sender_id:str=Form(...),
                   new_order:bool=Form(False),reply_message_id:int=Form(None),resume:bool=Form(False),session=Depends(session_dependency)):
        user=session.query(User).filter_by(telegram_id=str(sender_id)).first()
        if not accounts.eligible(session,user):raise HTTPException(403,'下单资格未通过')
        try:
            draft=accounts.open_message_draft(session,chat_id,message_id,sender_id,
                user.id,new_order=new_order,reply_message_id=reply_message_id,resume=resume)
            session.commit()
            return dict(draft=dict(draft,payload=json.loads(draft['payload'])) if draft else None)
        except ValueError as error:
            session.rollback();raise HTTPException(409,str(error)) from None

    @router.post('/api/telegram/draft/save')
    def draft_save(chat_id:str=Form(...),token:str=Form(...),payload:str=Form(...),sender_id:str=Form(...),
                   completed:bool=Form(False),session=Depends(session_dependency)):
        user=session.query(User).filter_by(telegram_id=str(sender_id)).first()
        if not accounts.eligible(session,user):raise HTTPException(403,'下单资格未通过')
        draft=accounts.select_draft(session,chat_id,user.id,'',token=token)
        if not draft:raise HTTPException(404,'订单草稿不存在或已完成')
        if len(payload.encode('utf-8')) > 262144:
            raise HTTPException(400,'草稿内容过大')
        try:
            value=json.loads(payload)
        except (ValueError, TypeError):
            raise HTTPException(400,'草稿格式无效') from None
        if not isinstance(value,dict):raise HTTPException(400,'草稿格式无效')
        reply_ids = value.get('_reply_message_ids',[])
        if not isinstance(reply_ids,list) or len(reply_ids)>100:
            raise HTTPException(400,'提示消息关联无效')
        for message_id in reply_ids:
            if not isinstance(message_id,int) or isinstance(message_id,bool) or message_id<=0:
                raise HTTPException(400,'提示消息关联无效')
            session.execute(text("INSERT INTO telegram_message_events(namespace,chat_id,message_id,sender_id,user_id,draft_token,status,created_at) VALUES(:namespace,:chat,:message,'BOT_PROMPT',:user,:token,'prompt',:now) ON CONFLICT(namespace,chat_id,message_id) DO NOTHING"),
                parameters(chat_id,message=message_id,user=user.id,token=token,now=accounts.datetime.now(accounts.timezone.utc).isoformat()))
        waiting='contact' if value.get('pending_recipient_order') else 'confirmation' if (value.get('pending_order') or value.get('pending_ai_flavor')) else 'manual_review' if value.get('pending_manual_review') else None
        saved=session.execute(text("UPDATE telegram_order_drafts SET payload=:payload,status=:status,waiting_field=:waiting,updated_at=:now WHERE token=:token AND status='pending'"),
            dict(payload=json.dumps(value,ensure_ascii=False),status='completed' if completed else 'pending',token=token,
                 waiting=waiting,now=accounts.datetime.now(accounts.timezone.utc).isoformat()))
        if saved.rowcount != 1:
            session.rollback();raise HTTPException(409,'订单草稿已完成或自动取消，未重新开启')
        session.commit();return dict(saved=True)

    @router.get('/api/telegram/draft/save-result')
    def draft_save_result(chat_id:str,token:str,sender_id:str,payload_sha256:str,
                          completed:bool=False,session=Depends(session_dependency)):
        # Read-only proof, including completed drafts. Never expose another
        # user's payload or reopen a completed draft after a transport timeout.
        user=session.query(User).filter_by(telegram_id=str(sender_id)).first()
        if not accounts.eligible(session,user):raise HTTPException(403,'下单资格未通过')
        if not re.fullmatch('[0-9a-f]{64}',payload_sha256):
            raise HTTPException(400,'草稿校验值无效')
        row=session.execute(text('SELECT payload,status FROM telegram_order_drafts '
            'WHERE namespace=:namespace AND chat_id=:chat AND user_id=:user AND token=:token'),
            parameters(chat_id,user=user.id,token=token)).mappings().first()
        if not row:return dict(saved=False)
        value=json.loads(row['payload'])
        digest=hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
            separators=(',',':')).encode('utf-8')).hexdigest()
        return dict(saved=digest==payload_sha256 and
            row['status']==('completed' if completed else 'pending'))

    @router.get('/api/telegram/draft')
    def get_draft(chat_id:str,token:str,sender_id:str,audit_event_id:int=None,
                  audit_action:str='',audit_message_id:int=None,include_flavor_expired:bool=False,session=Depends(session_dependency)):
        user=session.query(User).filter_by(telegram_id=str(sender_id)).first()
        authorized=accounts.eligible(session,user)
        from telegram_draft_audit import record
        row=session.execute(text('SELECT user_id,message_id,status FROM telegram_order_drafts WHERE namespace=:namespace AND chat_id=:chat AND token=:token'),parameters(chat_id,token=token)).mappings().first() if audit_event_id is not None else None
        if not authorized:
            record(audit_event_id,audit_action,user,chat_id,audit_message_id,row,False,False)
            raise HTTPException(403,'下单资格未通过')
        from flavor_draft_expiry import expire
        expire(session, chat_id=chat_id, user_id=user.id)
        session.commit()
        expired=session.execute(text("SELECT token,status FROM telegram_order_drafts WHERE namespace=:namespace AND chat_id=:chat AND user_id=:user AND token=:token AND status IN ('flavor_expired','confirmation_expired')"),parameters(chat_id,user=user.id,token=token)).mappings().first()
        if expired:
            record(audit_event_id,audit_action,user,chat_id,audit_message_id,row,True,False)
            if not include_flavor_expired:
                raise HTTPException(404,'订单确认已超时，请重新下单。')
            return dict(expired,payload={})
        draft=accounts.select_draft(session,chat_id,user.id,'',token=token)
        record(audit_event_id,audit_action,user,chat_id,audit_message_id,row,True,bool(draft))
        if not draft:raise HTTPException(404,'订单草稿不存在或已完成')
        return dict(draft,payload=json.loads(draft['payload']))

    @router.get('/api/telegram/proxy-confirmation-draft')
    def proxy_draft(chat_id:str,token:str,sender_id:str,session=Depends(session_dependency)):
        from proxy_confirmation import read_draft
        try:draft,owner=read_draft(session,sender_id,chat_id,token)
        except ValueError as error:raise HTTPException(403,str(error)) from None
        session.commit()
        draft['_proxy_account']=dict(authorized=True,user_id=owner.id,telegram_id=owner.telegram_id,username=owner.username)
        return draft

    return router
