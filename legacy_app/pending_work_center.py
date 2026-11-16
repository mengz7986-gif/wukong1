"""Read-only projection for the admin pending-work centre.

The projection deliberately does not replay messages or mutate drafts.  It turns
the durable queue and draft facts into a paginated operator view without calling
AI, Telegram, inventory, or order services.
"""
import json
import re
from datetime import datetime, timezone, timedelta
from sqlalchemy import text
from business_time_range import bounds, business_format


CATEGORIES = {
    'actionable': '等待客户点击',
    'contact': '等待收货信息',
    'confirmation': '等待订单确认',
    'manual_review': '人工核对',
    'failed': '处理失败／等待重试',
}

ACTIONABLE_TIMEOUT = timedelta(hours=2)


def _actionable_clause(alias='d'):
    prefix=alias+'.' if alias else ''
    payload=prefix+'payload'
    token=prefix+'token'
    field=prefix+'waiting_field'
    def value(path):return "json_extract("+payload+",'$."+path+"')"
    marker="json_extract("+payload+",'$._inline_confirmation.kind')"
    # Draft state and acknowledged ownership, not display age, determine
    # whether a prompt remains actionable.
    return f"""json_valid({payload}) AND (
      ({value('_inline_confirmation.active')}=1
       AND json_type({payload},'$._inline_confirmation.active')='true'
       AND {value('_inline_confirmation.delivery_acknowledged')}=1
       AND json_type({payload},'$._inline_confirmation.delivery_acknowledged')='true'
       AND {value('_inline_confirmation.token')}={token}
       AND json_type({payload},'$._inline_confirmation.message_id')='integer'
       AND {value('_inline_confirmation.message_id')}>0
       AND (({marker} IN ('name','address') AND json_type({payload},'$.pending_recipient_order')='object' AND {value('pending_recipient_order')}!='{{}}')
         OR ({marker}='flavor' AND json_type({payload},'$.pending_ai_flavor')='object' AND {value('pending_ai_flavor')}!='{{}}')
         OR ({marker}='order' AND json_type({payload},'$.pending_order')='object' AND {value('pending_order')}!='{{}}')))
      OR ({field}='confirmation' AND json_type({payload},'$._inline_confirmation') IS NULL
       AND COALESCE({value('_group_receipt_closed')},0)=0
       AND (json_type({payload},'$.pending_order')='object' OR json_type({payload},'$.pending_ai_flavor')='object')
       AND (COALESCE(json_array_length({value('_reply_message_ids')}),0)>0
            OR json_type({payload},'$._group_receipt_message_id')='integer')))
    """


def _payload_meta(payload, fallback_started=None):
    try:
        data=json.loads(payload or '{}')
    except (TypeError,ValueError):
        return {},None,None
    reply_ids=data.get('_reply_message_ids')
    receipt=data.get('_group_receipt_message_id')
    if not receipt and isinstance(reply_ids,list) and reply_ids:
        receipt=reply_ids[-1]
    pending=data.get('pending_ai_flavor') or data.get('pending_order') or {}
    raw_started=pending.get('_confirmation_started_at') if isinstance(pending,dict) else None
    try:
        started=datetime.fromisoformat(str(raw_started or fallback_started).replace('Z','+00:00'))
        if started.tzinfo is None:started=started.replace(tzinfo=timezone.utc)
        started=started.astimezone(timezone.utc)
    except (TypeError,ValueError):
        started=None
    return data,receipt if isinstance(receipt,int) and receipt>0 else None,started


def _message_link(chat_id,message_id):
    value=str(chat_id or '')
    if not message_id or not value.startswith('-100'):
        return None
    return f"https://t.me/c/{value[4:]}/{message_id}"


def _safe_text(value, limit=120):
    value = re.sub(r'\s+', ' ', str(value or '')).strip()
    value = re.sub(r'(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)', r'\1****\2', value)
    return value[:limit] + ('…' if len(value) > limit else '')


def _draft_detail(payload, waiting):
    try:
        data = json.loads(payload or '{}')
    except (TypeError, ValueError):
        return {'summary': '草稿内容无法解析', 'items': [], 'pending': '需人工核对草稿数据'}
    node = data.get('pending_recipient_order') or data.get('pending_order') or data.get('pending_ai_flavor') or data.get('pending_manual_review') or {}
    info = node.get('order_info') if isinstance(node, dict) else None
    info = info if isinstance(info, dict) else node if isinstance(node, dict) else {}
    raw = info.get('raw_text') or node.get('raw_text') if isinstance(node, dict) else ''
    raw = raw or info.get('text') or ''
    items = info.get('items') if isinstance(info.get('items'), list) else []
    clean_items = []
    for item in items[:20]:
        if not isinstance(item, dict):
            continue
        clean_items.append({'name': _safe_text(item.get('product_name') or item.get('name'), 50),
                            'quantity': item.get('quantity') or item.get('qty')})
    missing = []
    if waiting == 'contact':
        from contact_fields import diagnose_contact
        diagnosis=diagnose_contact(info)
        labels={'recipient':'姓名','phone':'电话','address':'地址'}
        missing=[labels.get(key,key) for key in diagnosis['missing']+diagnosis['invalid']]
    pending = '缺少：' + '、'.join(missing) if missing else CATEGORIES.get(waiting, '待处理')
    reasons = node.get('reasons') if isinstance(node, dict) else None
    if waiting == 'manual_review' and isinstance(reasons, list) and reasons:
        pending = '；'.join(_safe_text(x, 80) for x in reasons[:5])
    return {'summary': _safe_text(raw), 'items': clean_items, 'pending': pending}


def _utc_iso(value):
    if not value: return None
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is not None: parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.isoformat()


def list_work(session, *, category='', user='', chat='', start='', end='', page=1, page_size=25):
    if category and category not in CATEGORIES: raise ValueError('待处理分类无效')
    page=max(1,int(page)); page_size=max(1,min(100,int(page_size)))
    first,last=bounds(start,end)
    clauses=["d.status='pending'"]; params={}
    if category=='actionable':
        clauses.append(_actionable_clause())
    elif category and category!='failed':
        clauses.append("COALESCE(d.waiting_field,'manual_review')=:category");params['category']=category
    if user: clauses.append("(CAST(d.user_id AS TEXT)=:user OR u.username LIKE :like_user OR u.display_name LIKE :like_user OR u.telegram_name LIKE :like_user)");params.update(user=str(user),like_user='%'+str(user)+'%')
    if chat: clauses.append('d.chat_id=:chat');params['chat']=str(chat)
    if first: clauses.append('d.updated_at>=:first');params['first']=first.isoformat()
    if last: clauses.append('d.updated_at<:last');params['last']=last.isoformat()
    offset=(page-1)*page_size
    draft_rows=[]; draft_total=0
    if category!='failed':
        where=' AND '.join(clauses)
        draft_total=session.execute(text('SELECT COUNT(*) FROM telegram_order_drafts d LEFT JOIN users u ON u.id=d.user_id WHERE '+where),params).scalar() or 0
        query='''SELECT d.namespace,d.token,d.chat_id,d.message_id,d.sender_id,d.user_id,d.waiting_field,
            d.payload,d.created_at,d.updated_at,u.username,u.display_name,u.telegram_name
            FROM telegram_order_drafts d LEFT JOIN users u ON u.id=d.user_id WHERE '''+where+''' ORDER BY d.updated_at DESC,d.token DESC LIMIT :limit OFFSET :offset'''
        params.update(limit=page_size,offset=offset)
        draft_rows=session.execute(text(query),params).mappings().all()
    failed_params={'limit':page_size,'offset':offset}
    failed_clauses=["j.state IN ('REVIEW_REQUIRED','FAILED_RETRYABLE')"]
    if chat: failed_clauses.append('j.chat_id=:fchat');failed_params['fchat']=str(chat)
    if first: failed_clauses.append('j.received_at>=:ffirst');failed_params['ffirst']=first.replace(tzinfo=timezone.utc).timestamp()
    if last: failed_clauses.append('j.received_at<:flast');failed_params['flast']=last.replace(tzinfo=timezone.utc).timestamp()
    failed_rows=[]; failed_total=0
    if category in ('','failed') and not user:
        fwhere=' AND '.join(failed_clauses)
        failed_total=session.execute(text('SELECT COUNT(*) FROM telegram_order_jobs j WHERE '+fwhere),failed_params).scalar() or 0
        if category=='failed':
            failed_rows=session.execute(text('SELECT namespace,chat_id,message_id,sender_id,state,attempts,reason,received_at,started_at,finished_at FROM telegram_order_jobs j WHERE '+fwhere+' ORDER BY received_at DESC LIMIT :limit OFFSET :offset'),failed_params).mappings().all()
        elif category=='' and len(draft_rows)<page_size:
            failed_params['limit']=page_size-len(draft_rows)
            failed_params['offset']=max(0,offset-draft_total)
            failed_rows=session.execute(text('SELECT namespace,chat_id,message_id,sender_id,state,attempts,reason,received_at,started_at,finished_at FROM telegram_order_jobs j WHERE '+fwhere+' ORDER BY received_at DESC LIMIT :limit OFFSET :offset'),failed_params).mappings().all()
    rows=[]
    for row in draft_rows:
        detail=_draft_detail(row['payload'],row['waiting_field'])
        payload,receipt_id,started=_payload_meta(row['payload'],row['updated_at'])
        from draft_button_evidence import proven_kind
        button_kind=proven_kind(payload,row['token'])
        if button_kind:
            receipt_id=payload['_inline_confirmation']['message_id']
            detail['pending']={'address':'等待客户确认地址','name':'等待客户确认姓名','flavor':'等待客户确认商品口味','order':'等待客户确认订单'}[button_kind]
        updated=datetime.fromisoformat(str(row['updated_at']).replace('Z','+00:00'))
        if updated.tzinfo is None: updated=updated.replace(tzinfo=timezone.utc)
        now=datetime.now(timezone.utc)
        waiting_seconds=max(0,int((now-(started or updated.astimezone(timezone.utc))).total_seconds()))
        remaining_seconds=max(0,int((((started or updated.astimezone(timezone.utc))+ACTIONABLE_TIMEOUT)-now).total_seconds()))
        version_row=dict(row,status='pending')
        from pending_work_actions import version as draft_version
        rows.append(dict(kind='draft',category=row['waiting_field'] or 'manual_review',category_label=({'name':'姓名确认','flavor':'口味确认','order':'订单确认'}.get(button_kind) or CATEGORIES.get(row['waiting_field'],'人工核对')),button_kind=button_kind,
            namespace=row['namespace'],token=row['token'],chat_id=row['chat_id'],message_id=row['message_id'],user_id=row['user_id'],
            user_name=row['display_name'] or row['telegram_name'] or row['username'] or str(row['sender_id']),status='pending',
            version=draft_version(version_row),created_at=business_format(row['created_at']),updated_at=business_format(row['updated_at']),
            receipt_message_id=receipt_id,message_link=_message_link(row['chat_id'],receipt_id),waiting_seconds=waiting_seconds,
            remaining_seconds=remaining_seconds,error='',**detail))
    for row in failed_rows:
        stamp=datetime.fromtimestamp(row['received_at'],timezone.utc)
        rows.append(dict(kind='source_job',category='failed',category_label=CATEGORIES['failed'],namespace=row['namespace'],token='',chat_id=row['chat_id'],message_id=row['message_id'],user_id=None,user_name=str(row['sender_id']),status=row['state'],attempts=row['attempts'],created_at=business_format(stamp),updated_at=business_format(datetime.fromtimestamp(row['finished_at'] or row['started_at'] or row['received_at'],timezone.utc)),summary='来源内容受保护，请逐条核查',items=[],pending='核对失败原因后决定是否恢复',error=_safe_text(row['reason'],200)))
    counts=dict(session.execute(text("SELECT COALESCE(waiting_field,'manual_review'),COUNT(*) FROM telegram_order_drafts WHERE status='pending' GROUP BY COALESCE(waiting_field,'manual_review')")).all())
    counts['failed']=session.execute(text("SELECT COUNT(*) FROM telegram_order_jobs WHERE state IN ('REVIEW_REQUIRED','FAILED_RETRYABLE')")).scalar() or 0
    actionable=session.execute(text("SELECT COUNT(*) FROM telegram_order_drafts d WHERE d.status='pending' AND "+_actionable_clause())).scalar() or 0
    counts['actionable']=actionable
    return {'rows':rows,'counts':{key:int(counts.get(key,0)) for key in CATEGORIES},'total':int(failed_total if category=='failed' else draft_total+failed_total),'page':page,'page_size':page_size,
            'scope':{'category':category,'user':user,'chat':chat,'start':start,'end':end}}
