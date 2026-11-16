"""Idempotent admin lifecycle actions for unsubmitted Telegram drafts."""
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import text


def migrate(connection):
    connection.execute(text('''CREATE TABLE IF NOT EXISTS telegram_pending_admin_audit(
        operation_key TEXT PRIMARY KEY, draft_token TEXT NOT NULL, action TEXT NOT NULL,
        operator TEXT NOT NULL, reason TEXT NOT NULL, before_version TEXT NOT NULL,
        result TEXT NOT NULL, created_at TEXT NOT NULL)'''))


def _row(session, token):
    row=session.execute(text('SELECT * FROM telegram_order_drafts WHERE token=:token'),{'token':token}).mappings().first()
    if not row: raise ValueError('草稿不存在')
    return dict(row)


def version(row):
    body=[row.get(k) for k in ('namespace','token','chat_id','message_id','sender_id','user_id','status','waiting_field','payload','updated_at')]
    return hashlib.sha256(json.dumps(body,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def close_draft(database, *, token, expected_version, operator, reason, operation_key):
    if not operator: raise ValueError('缺少操作员')
    reason=str(reason or '').strip()
    if not reason or len(reason)>500: raise ValueError('关闭原因必填，最多500字')
    if not isinstance(operation_key,str) or not 16<=len(operation_key)<=100: raise ValueError('操作编号无效')
    with database.SessionLocal() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        migrate(session.connection())
        prior=session.execute(text('SELECT * FROM telegram_pending_admin_audit WHERE operation_key=:key'),{'key':operation_key}).mappings().first()
        if prior:
            if prior['draft_token']!=token or prior['operator']!=operator or prior['reason']!=reason:
                raise ValueError('操作编号已用于其他请求')
            return json.loads(prior['result'])
        row=_row(session,token)
        if version(row)!=expected_version: raise ValueError('草稿已变化，请刷新后重试')
        if row['status']!='pending': raise ValueError('草稿已处理或已过期')
        order=session.execute(text('''SELECT o.id FROM orders o JOIN telegram_message_events e
            ON CAST(o.source_chat_id AS TEXT)=e.chat_id AND o.source_message_id=e.message_id
            WHERE e.namespace=:namespace AND e.draft_token=:token LIMIT 1'''),
            {'namespace':row['namespace'],'token':row['token']}).first()
        if order: raise ValueError('该来源已关联正式订单，不能从草稿入口关闭')
        now=datetime.now(timezone.utc).isoformat()
        changed=session.execute(text("UPDATE telegram_order_drafts SET status='admin_closed',waiting_field=NULL,updated_at=:now WHERE token=:token AND status='pending' AND updated_at=:updated"),
            {'now':now,'token':token,'updated':row['updated_at']})
        if changed.rowcount!=1: raise ValueError('草稿已变化，未关闭')
        session.execute(text('''UPDATE telegram_order_jobs SET state='REJECTED',reason='DRAFT_CLOSED',
            lease_token=NULL,lease_until=NULL,finished_at=:finished WHERE state='NEED_CONFIRMATION'
            AND namespace=:namespace AND EXISTS(SELECT 1 FROM telegram_message_events e
              WHERE e.namespace=telegram_order_jobs.namespace AND e.chat_id=telegram_order_jobs.chat_id
              AND e.message_id=telegram_order_jobs.message_id AND e.draft_token=:token)'''),
            {'finished':datetime.now(timezone.utc).timestamp(),'namespace':row['namespace'],'token':token})
        result={'token':token,'status':'admin_closed','formal_orders_changed':0,'notifications_sent':0}
        session.execute(text('INSERT INTO telegram_pending_admin_audit VALUES(:key,:token,\'close_draft\',:operator,:reason,:version,:result,:created)'),
            {'key':operation_key,'token':token,'operator':operator,'reason':reason,'version':expected_version,
             'result':json.dumps(result,ensure_ascii=False),'created':now})
        session.commit()
        return result


def supplement_contact(database, *, token, expected_version, operator, reason, operation_key,
                       recipient='', phone='', address=''):
    """Correct contact facts on a pending draft without confirming or creating an order."""
    if not operator: raise ValueError('缺少操作员')
    reason=str(reason or '').strip()
    if not reason or len(reason)>500: raise ValueError('修改原因必填，最多500字')
    if not isinstance(operation_key,str) or not 16<=len(operation_key)<=100: raise ValueError('操作编号无效')
    supplied={key:str(value or '').strip() for key,value in
              (('recipient',recipient),('phone',phone),('address',address)) if str(value or '').strip()}
    if not supplied: raise ValueError('请至少填写一项收货信息')
    limits={'recipient':100,'phone':30,'address':500}
    if any(len(value)>limits[key] or any(ord(ch)<32 for ch in value) for key,value in supplied.items()):
        raise ValueError('收货信息长度或格式无效')
    with database.SessionLocal() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE');migrate(session.connection())
        prior=session.execute(text('SELECT * FROM telegram_pending_admin_audit WHERE operation_key=:key'),{'key':operation_key}).mappings().first()
        if prior:
            if prior['draft_token']!=token or prior['operator']!=operator or prior['reason']!=reason:
                raise ValueError('操作编号已用于其他请求')
            return json.loads(prior['result'])
        row=_row(session,token)
        if version(row)!=expected_version: raise ValueError('草稿已变化，请刷新后重试')
        if row['status']!='pending' or row.get('waiting_field')!='contact':
            raise ValueError('当前草稿不在等待收货信息状态')
        payload=json.loads(row.get('payload') or '{}')
        pending=payload.get('pending_recipient_order')
        if not isinstance(pending,dict) or not isinstance(pending.get('order_info'),dict):
            raise ValueError('草稿结构不完整，不能直接修改')
        order_info=dict(pending['order_info']);order_info.update(supplied);pending['order_info']=order_info
        pending.setdefault('admin_supplements',[]).append({'fields':sorted(supplied),'operator':operator,
            'at':datetime.now(timezone.utc).isoformat(),'reason':reason})
        from contact_fields import diagnose_contact
        diagnosis=diagnose_contact(order_info)
        now=datetime.now(timezone.utc).isoformat();encoded=json.dumps(payload,ensure_ascii=False)
        changed=session.execute(text("UPDATE telegram_order_drafts SET payload=:payload,updated_at=:now WHERE token=:token AND status='pending' AND waiting_field='contact' AND updated_at=:updated"),
            {'payload':encoded,'now':now,'token':token,'updated':row['updated_at']})
        if changed.rowcount!=1:raise ValueError('草稿已变化，未修改')
        result={'token':token,'status':'pending','remaining_missing':diagnosis['missing'],
            'remaining_invalid':diagnosis['invalid'],'formal_orders_changed':0,'notifications_sent':0}
        session.execute(text('INSERT INTO telegram_pending_admin_audit VALUES(:key,:token,\'supplement_contact\',:operator,:reason,:version,:result,:created)'),
            {'key':operation_key,'token':token,'operator':operator,'reason':reason,'version':expected_version,
             'result':json.dumps(result,ensure_ascii=False),'created':now})
        session.commit();return result
