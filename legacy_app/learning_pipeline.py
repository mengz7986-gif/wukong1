"""Post-commit observations only; non-critical side effect outside order writes."""
import hashlib,hmac,json


def record_committed(preview,order):
    if not isinstance(order,dict) or not (order.get('id') or order.get('order_number') or order.get('order_numbers')):
        raise ValueError('缺少已提交订单凭证，不能学习')
    from config import SESSION_SECRET_KEY
    identity=[preview.get('source_chat_id'),preview.get('source_message_id'),order.get('id'),order.get('order_number'),order.get('order_numbers')]
    event=hmac.new(SESSION_SECRET_KEY.encode(),json.dumps(identity,sort_keys=True).encode(),hashlib.sha256).hexdigest()
    # Address hints are independent of generated recipient names. Only the
    # address is stored, encrypted and scoped to this account, after commit.
    try:
        from verified_address_cache import remember_committed
        remember_committed(preview,order,event)
    except Exception:
        # Cache availability must never change a committed order's outcome.
        pass
    trace_id=preview.get('_order_trace_id')
    if trace_id:
        try:
            from ai_usage_ledger import associate_commit
            count=len(order.get('order_numbers') or []) or 1
            associate_commit(trace_id=trace_id,commit_fingerprint=event,order_count=count)
        except Exception:
            # Usage association is post-commit telemetry and cannot alter the
            # order. Missing association remains explicit in the ledger.
            pass
        try:
            from ai_diagnostics import associate_order
            numbers=order.get('order_numbers') or [order.get('order_number')]
            associate_order(trace_id,numbers)
        except Exception:
            pass
    # Temporary administrator labels are not customer knowledge. Preserve
    # post-commit usage association above but create no learning candidate,
    # Alias, raw replay evidence or daily-review material from such an order.
    if preview.get('_recipient_generated') or preview.get('_recipient_learning_excluded'):
        return event
    evidence=preview.get('_learning_evidence')
    if evidence:
        from brand_aliases import _learn_from_committed
        _learn_from_committed(evidence['result'],preview,evidence['catalog'],event)
    from learning_knowledge import observe
    for kind,form in preview.get('_learning_forms',[]):
        # Layout habits are customer-specific. Grammar/address facts are only
        # global candidates and still require human review before consumption.
        scope='USER' if kind=='HABIT' else 'GLOBAL'
        candidate=observe(kind=kind,form=form,commit_fingerprint=event,proof='COMMITTED_VALIDATED',
            scope=scope,principal=preview.get('telegram_id') if scope=='USER' else None)
        try:
            from learning_evidence import record
            record(candidate_id=candidate['id'],commit_fingerprint=event,kind=kind,form=form,
                   preview=preview,order=order)
        except Exception:
            # Evidence enrichment is non-critical and may never change a
            # committed order or approve knowledge automatically.
            pass
    return event


def forms_from_validated_order(info):
    import re
    forms=set()
    mapping={'ITEM_QUANTITY':'ITEM_SUFFIX','SECTION_SUBTOTAL':'SECTION_SUBTOTAL','ORDER_TOTAL':'ORDER_TOTAL'}
    for fact in info.get('quantity_scopes') or []:
        if fact.get('kind') in mapping:forms.add(('QUANTITY',mapping[fact['kind']]))
    if info.get('customer_amount_notes'):forms.add(('QUANTITY','AMOUNT_EXPRESSION'))
    if info.get('fulfillment_notes'):forms.add(('ALTERNATE','EXPLICIT_BACKUP'))
    if info.get('address'):forms.add(('ADDRESS','EXPLICIT_ADMIN'))
    raw=str(info.get('raw_text') or '')
    from learning_knowledge import _CN_QUANTITY
    if re.search(r'(?m)^\s*.{1,80}?[(（]'+_CN_QUANTITY+r'(?:支|根|件|个|条)[)）]\s*$',raw):
        forms.add(('HABIT','ITEM_CHINESE_QUANTITY'))
    contact=re.search(r'地址|收件|姓名|电话|手机',raw)
    goods=re.search(r'商品|产品|口味|[×xX*]\s*\d+|\d+\s*(?:支|根|件|个)',raw)
    if contact and goods:
        forms.add(('HABIT','CONTACT_BEFORE_PRODUCT' if contact.start()<goods.start() else 'PRODUCT_BEFORE_CONTACT'))
    if len(info.get('section_subtotals') or [])>1:forms.add(('HABIT','MULTI_SECTION_ORDER'))
    return sorted(forms)
