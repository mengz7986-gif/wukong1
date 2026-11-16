"""Preserve explicitly written administrative nodes at final submission."""
import re
from address_utils import normalize_cn_address
from contact_fields import parse_contact


def prefer_complete_source_address(raw, parsed_address, candidate_address, products=(), item_tokens=()):
    """Use a shorter candidate only when the source proves an exact boundary."""
    parsed=str(parsed_address or '').strip();candidate=str(candidate_address or '').strip()
    if not parsed or not candidate or parsed==candidate or not parsed.startswith(candidate):return parsed
    occurrences=[m for m in re.finditer(re.escape(candidate),str(raw or ''))]
    if len(occurrences)!=1:return parsed
    end=occurrences[0].end();remainder=str(raw)[end:]
    if remainder.split('\n',1)[0].strip():return parsed
    following=[line.strip() for line in remainder.splitlines()[1:] if line.strip()]
    if not following:return parsed
    live=[p for p in products or () if p.get('is_active',True) and not p.get('is_archived')]
    flavor_tokens={str(p.get('name') or '').strip().casefold() for p in live}
    category_tokens={str(p.get('parent_name') or '').strip().casefold() for p in live}
    # Only an already-reviewed, catalog-bound Alias may prove a merchandise
    # boundary. Provider raw tokens are deliberately ignored.
    try:
        from rule_dictionary import read_store
        for row in read_store().get('aliases',[]):
            alias=str(row.get('alias') or '').strip().casefold()
            if not alias or row.get('status')!='active':continue
            if row.get('type')=='EXPLICIT_FLAVOR':
                valid=any(
                    str(row.get('standard') or '')==str(product.get('name') or '')
                    and str(row.get('category') or '')==str(product.get('parent_name') or '')
                    and (not row.get('product_id') or int(row['product_id'])==int(product.get('id') or 0))
                    and (not row.get('category_id') or int(row['category_id'])==int(product.get('parent_id') or 0))
                    for product in live)
                if valid:flavor_tokens.add(alias)
            elif row.get('type')=='MODEL_ALIASES':
                valid=any(
                    str(row.get('standard') or '')==str(product.get('parent_name') or '')
                    and (not row.get('category_id') or int(row['category_id'])==int(product.get('parent_id') or 0))
                    for product in live)
                if valid:category_tokens.add(alias)
    except Exception:
        pass
    evidence_line=following[0].casefold()
    flavors='|'.join(re.escape(token) for token in sorted(filter(None,flavor_tokens),key=len,reverse=True))
    categories='|'.join(re.escape(token) for token in sorted(filter(None,category_tokens),key=len,reverse=True))
    if not flavors:return parsed
    category_prefix=(r'(?:(?:'+categories+r')\s*(?:[xX×*]\s*\d+\s*(?:支|根|件|个)?)?\s*[:：]?\s*)?' if categories else '')
    item=r'(?:'+flavors+r')\s*(?:[xX×*:：]\s*)?[1-9]\d*\s*(?:支|根|件|个)?'
    whole=r'^\s*'+category_prefix+item+r'(?:\s*(?:[,，、;；]\s*|\s+)'+item+r')*\s*$'
    if not re.fullmatch(whole,evidence_line):return parsed
    validate_explicit_region(raw,candidate,products)
    return candidate


@__import__('order_trace').timed_sync('CURRENT_REGION')
def validate_current_region(address):
    from cloud_address_trust import trusted
    if trusted(address):return
    meta=normalize_cn_address(address)
    if meta.get('named_parent_conflict'):
        raise ValueError('❌ 录单失败\n所填省市区与已核实行政名称的上级关系冲突；请核对完整地址后重新下单，未自动改写原文。')
    if meta.get('unverified_city_scope'):
        raise ValueError('❌ 录单失败\n该城市尚未在当前地址数据中完成核验，暂不能自动录单；请联系客服核实，不要重复提交。')
    if meta.get('legacy_region_conflict'):
        raise ValueError('❌ 录单失败\n原区县名称与当前行政区快照不一致；请核实当前区县后重新发送完整订单，不能自动猜测替换旧地名。')
    from offline_address import assess,problem_text
    decision=assess(address)
    if not decision['valid']:
        raise ValueError('❌ 录单失败\n'+problem_text(dict(address_issues=decision['issues'])))


@__import__('order_trace').timed_sync('SOURCE_REGION_EVIDENCE')
def validate_persisted_region(session,chat_id,message_id,submitted_address):
    """Independent address-only transaction guard; no fulfillment policy."""
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
        raise RuntimeError('ADDRESS_SOURCE_EVIDENCE_UNAVAILABLE') from exc
    message=payload.get('message') or {}
    raw=message.get('text') or message.get('caption') or ''
    if not raw:return
    from models import Product
    from sqlalchemy.orm import selectinload
    catalog=[dict(id=p.id,name=p.name,parent_name=p.parent.name if p.parent else '',
                  parent_id=p.parent_id,default_warehouse_id=p.default_warehouse_id,
                  is_active=p.is_active,is_archived=p.is_archived)
             for p in session.query(Product).options(selectinload(Product.parent)).filter_by(is_parent=False)]
    validate_explicit_region(raw,submitted_address,catalog)


def _region_key(raw,submitted_address,products=()):
    from region_index import get_region_index
    from cloud_address_trust import trusted
    fields=('id','name','parent_name','parent_id','default_warehouse_id','is_active','is_archived')
    return (raw,submitted_address,tuple(tuple(p.get(k) for k in fields) for p in products),
            get_region_index(),bool(trusted(submitted_address)))


from request_memo import request_memoized

@request_memoized(_region_key)
def validate_explicit_region(raw, submitted_address, products=()):
    validate_current_region(submitted_address)
    original=parse_contact(raw,products=products)
    source=str(original.get('raw_address') or '')
    expected=normalize_cn_address(source)
    actual=normalize_cn_address(submitted_address)
    from cloud_address_trust import trusted
    cloud_trusted=trusted(submitted_address)
    if not cloud_trusted and (actual.get('legacy_region_conflict') or expected.get('legacy_region_conflict')):
        raise ValueError('❌ 录单失败\n原区县名称与当前行政区快照不一致；请核实当前区县后重新发送完整订单，不能自动猜测替换旧地名。')
    # Only exact administrative labels actually present in the customer's
    # delivery span are constraints; an inferred node or POI city stem is not.
    explicit={key:expected[key] for key in ('province','city','district')
              if expected.get(key) and expected[key] in source}
    conflicts=[key for key,value in explicit.items() if actual.get(key)!=value and (not cloud_trusted or key in ('province','city'))]
    if conflicts:
        raise ValueError('❌ 录单失败\n收货地址与原文明确的省市区不一致；请核对地址后重新发送完整订单。')
    detail=''.join(str(expected.get('detail') or '').split()).casefold()
    actual_text=''.join(str(actual.get('normalized_address') or '').split()).casefold()
    if detail and detail not in actual_text:
        raise ValueError('❌ 录单失败\n原文详细收货地址未完整保留（楼栋、单元、房号等）；请核对后重新发送完整订单，未自动删改地址。')
    # Final persistence guard: a catalog token present in the order body but
    # absent from the source-proven address may never appear in the submitted
    # address. This catches pollution introduced after Contact parsing (AI
    # merge, build request or receipt mapping) without deleting a genuine
    # place name that the customer actually wrote inside the address span.
    compact_source=''.join(source.split()).casefold()
    compact_submitted=''.join(str(submitted_address or '').split()).casefold()
    compact_raw=''.join(str(raw or '').split()).casefold()
    live=[p for p in products or () if p.get('is_active',True) and not p.get('is_archived')]
    # A glued ``category + flavor + quantity`` tail is merchandise even when
    # the first contact pass accidentally included the whole tail in its
    # address span.  Require both catalog identities plus a quantity suffix;
    # a single catalog word may still be a genuine POI/street name.
    glued=[]
    for product in live:
        category=''.join(str(product.get('parent_name') or '').split()).casefold()
        flavor=''.join(str(product.get('name') or '').split()).casefold()
        if not category or not flavor:continue
        pattern=re.escape(category)+r'\s*'+re.escape(flavor)+r'(?:[×xX*]?\d+|[一二两三四五六七八九十]+)\s*(?:支|根|件|个)?$'
        from catalog_regex import compiled
        if compiled(pattern).search(compact_submitted):glued.append(category+'/'+flavor)
    if glued:
        raise ValueError('❌ 录单失败\n收货地址中混入了商品或口味内容；本次未录入，请重新发送完整订单。')
    identities={str(p.get(key) or '').strip() for p in live for key in ('name','parent_name')}
    polluted=[token for token in identities
              if len(token)>=2 and token.casefold() in compact_raw
              and token.casefold() not in compact_source
              and token.casefold() in compact_submitted]
    if polluted:
        raise ValueError('❌ 录单失败\n收货地址中混入了商品或口味内容；本次未录入，请重新发送完整订单。')
