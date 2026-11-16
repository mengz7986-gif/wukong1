"""Catalog-scoped source count coverage; no stock, account or network writes."""
import re
from collections import Counter

@__import__('order_trace').timed_sync('SOURCE_QUANTITY_EVIDENCE')
def validate_persisted_source_total(session,chat_id,message_id,items,*,bundle_requests=None,recipient_address=None):
    """Read original encrypted source inside final write transaction; never replay."""
    if chat_id is None or message_id is None:return
    from sqlalchemy import inspect,text
    from telegram_source_identity import parameters
    if not inspect(session.connection()).has_table('telegram_order_jobs'):return
    encrypted=session.execute(text('SELECT payload FROM telegram_order_jobs WHERE namespace=:namespace AND chat_id=:chat AND message_id=:message'),
        parameters(chat_id,message=message_id)).scalar()
    if not encrypted:return
    import json
    from config import SESSION_SECRET_KEY
    from telegram_order_queue import Queue
    try:
        payload=json.loads(Queue(None,SESSION_SECRET_KEY).cipher.decrypt(encrypted.encode()))
    except Exception as exc:
        raise RuntimeError('SOURCE_CONSERVATION_EVIDENCE_UNAVAILABLE') from exc
    original=payload.get('message') or {}
    raw=original.get('text') or original.get('caption') or ''
    if not raw:return
    from models import Product
    from sqlalchemy.orm import selectinload
    catalog=[dict(id=p.id,name=p.name,parent_name=p.parent.name if p.parent else '',parent_id=p.parent_id,
                  default_warehouse_id=p.default_warehouse_id,is_active=p.is_active,is_archived=p.is_archived)
             for p in session.query(Product).options(selectinload(Product.parent)).filter_by(is_parent=False)]
    return _validate_source_total(raw,catalog,items,bundle_requests=bundle_requests,recipient_address=recipient_address)


def _source_key(raw,catalog,items,*,bundle_requests=None,recipient_address=None):
    # Optional combined address checks have external trust state; retain
    # their original revalidation path rather than caching that decision.
    if recipient_address is not None:return None
    import json
    from region_index import get_region_index
    try:return (raw,json.dumps([catalog,items,bundle_requests,recipient_address],sort_keys=True,ensure_ascii=False),get_region_index())
    except (TypeError,ValueError):return None


from request_memo import request_memoized

@request_memoized(_source_key)
def _validate_source_total(raw,catalog,items,*,bundle_requests=None,recipient_address=None):
    """Memoize only pure evidence checks; callers always reread source/catalog."""
    if recipient_address is not None:
        from address_source_guard import validate_explicit_region
        validate_explicit_region(raw,recipient_address,catalog)
    if bundle_requests:return
    from order_structure import quantity_text
    from catalog_quantity_scopes import analyze
    source=quantity_text(raw,catalog)
    scope=analyze(source,products=catalog,ignore_leading_bare_capacity=True)
    expected=sum(f.quantity for f in scope.facts if f.kind=='ITEM_QUANTITY')
    submitted=sum(int(row.get('quantity') or 0) for row in items)
    errors=list(scope.errors)
    from order_structure import structure_order_text
    if submitted and not expected and structure_order_text(raw,catalog).customer_amount_notes:
        errors.append('商品数量未明确，收件信息后的金额不能作为数量。')
    if expected and expected!=submitted:
        errors.append(f'原文商品合计{expected}支，本次明细仅{submitted}支。')
    if scope.declared_total is not None and scope.declared_total!=submitted:
        errors.append(f'原文总数{scope.declared_total}支，本次明细{submitted}支。')
    categories={str(p['parent_name']).casefold():str(p['parent_name']) for p in catalog if p['parent_name']}
    source_categories=Counter()
    for fact in scope.facts:
        category=categories.get(str(fact.section or '').casefold())
        if fact.kind=='ITEM_QUANTITY' and category:
            source_categories[category]+=fact.quantity
    submitted_categories=Counter()
    products_by_id={p['id']:p for p in catalog}
    for row in items:
        product=products_by_id.get(row.get('product_id'))
        if product:
            submitted_categories[product['parent_name']]+=int(row.get('quantity') or 0)
    for category,count in source_categories.items():
        if submitted_categories[category]!=count:
            errors.append(f'品类({category})原文{count}支，本次明细{submitted_categories[category]}支。')
    # Known bare flavours have already become explicit ×1 facts.  Remaining
    # entries are unresolved category/identity evidence, not a reason to
    # override the confirmed default-quantity rule.
    ambiguous=unquantified_flavors(source,catalog)
    if ambiguous:errors.append('口味所属品类或商品无法确认：'+'、'.join(ambiguous))
    if errors:raise ValueError('❌ 录单失败\n'+'\n'.join('• '+error for error in dict.fromkeys(errors))+'\n请核对全部商品和数量后重新发送完整订单。')

def unquantified_flavors(source,products):
    """Retain a whole scoped merchandise line when per-flavor counts lack proof."""
    from order_parser import OrderParser
    unresolved=[]
    categories={str(p.get('parent_name') or '') for p in products if p.get('is_active',True) and not p.get('is_archived')}
    lines=str(source or '').splitlines()
    # Even an unreviewed model spelling can declare a section total. It does
    # not prove the allocation in a following bare live-flavor list. Keep this
    # check independent of heading resolution so AI cannot manufacture each=1.
    for index,line in enumerate(lines[:-1]):
        if not re.search(r'\d+\s*(?:个|支|根|件)\s*$',line):continue
        if re.search(r'1[3-9]\d{9}|地址|省|市|区|县|路|街|楼|栋|室',line):continue
        tokens=[token for token in re.split(r'[\s,，、;；]+',lines[index+1].strip()) if token]
        if len(tokens)<2:continue
        for category in categories:
            names={str(p.get('name') or '').casefold() for p in products
                   if p.get('parent_name')==category and p.get('is_active',True) and not p.get('is_archived')}
            if all(token.casefold() in names for token in tokens):
                # The heading was not resolved, so do not assign an arbitrary
                # category when several categories share these flavor names.
                unresolved.extend(tokens)
    for line in lines:
        if re.search(r'1[3-9]\d{9}|省|市|区|县|地址|备选|备用',line):continue
        for category in categories:
            if not category or category.casefold() not in line.casefold():continue
            # A count before the model cannot prove counts for a following
            # multi-flavor list, including unknown/unreviewed flavor words.
            leading=re.match(r'^\s*\d+\s*(?:个|支|根|件)\s*'+re.escape(category)+r'(?=\s|[:：])\s*[:：]?\s*(.+)$',line,re.I)
            if leading:
                tokens=[token for token in re.split(r'[\s,，、;；]+',leading.group(1)) if token]
                explicit=lambda token:bool(re.fullmatch(r'.+?[×xX*]?\d+(?:个|支|根|件)?',token)
                                           or re.fullmatch(r'\d+(?:个|支|根|件).+',token))
                if len(tokens)>1 and not all(explicit(token) for token in tokens):
                    unresolved.extend(category+'·'+token for token in tokens)
                    continue
            names=sorted({str(p['name']) for p in products if p.get('parent_name')==category
                          and p.get('is_active',True) and not p.get('is_archived')},key=len,reverse=True)
            found=[];covered=[]
            for name in names:
                for match in re.finditer(re.escape(name),line,re.I):
                    if any(a<=match.start()<b for a,b in covered):continue
                    found.append((name,match));covered.append(match.span())
            if len(found)<2:continue
            found.sort(key=lambda item:item[1].start())
            missing=[]
            for name,match in found:
                tail=line[match.end():]
                before=line[:match.start()]
                explicit=bool(re.match(r'\s*(?:[×xX*]\s*)?\d+\s*(?:个|支|根|件)?(?=$|[\s,，、;；])',tail)
                              or re.search(r'\d+\s*(?:个|支|根|件)\s*$',before))
                if not explicit:missing.append(category+'·'+name)
            if missing:unresolved.extend(category+'·'+name for name,_ in found)
    return list(dict.fromkeys(unresolved))

def catalog_quantity_errors(facts, items, products):
    live=[p for p in products if p.get('is_active',True) and not p.get('is_archived')]
    expected=Counter();actual=Counter()
    def identity(category,flavor):
        matches={(str(p.get('parent_name') or ''),str(p.get('name') or '')) for p in live
                 if str(p.get('name') or '').casefold()==flavor.casefold()
                 and (not category or str(p.get('parent_name') or '').casefold()==category.casefold())}
        return next(iter(matches)) if len(matches)==1 else None
    for fact in facts:
        if fact.kind!='ITEM_QUANTITY':continue
        key=identity(str(fact.section or ''),str(fact.item or ''))
        if key:expected[key]+=fact.quantity
    for ref,qty in items:
        match=re.fullmatch(r'品类\((.+)\)口味\((.+)\)',str(ref))
        key=identity(match.group(1),match.group(2)) if match else identity('',str(ref))
        if key:actual[key]+=int(qty)
    return [f'商品明细与原文不一致：{category}/{flavor}原文{qty}，识别{actual[key]}，请确认整单。'
            for key,qty in expected.items() for category,flavor in [key] if actual[key]!=qty]
