"""Dropshipping contact extraction, field merge and business diagnostics.

Uses the existing delivery/administrative parsers; never parses merchandise.
"""
import re
import unicodedata
from catalog_regex import compiled

from address_utils import normalize_cn_address

FIELDS = ('recipient', 'phone', 'address')

def contact_evidence_summary(fields, source=''):
    """Content-free contact shape, not a recoverable customer/AI snapshot."""
    is_object=isinstance(fields,dict)
    fields=fields if isinstance(fields,dict) else {}
    values={key:fields.get(key) if isinstance(fields.get(key),str) else '' for key in FIELDS}
    status=diagnose_contact(values)
    name=values['recipient'];phone=values['phone'];address=values['address']
    origin=fields.get('_recipient_source')
    return dict(object=is_object,
        fields={key:dict(present=bool(value),characters=min(len(value),1000000),
                        valid=key in status['recognized']) for key,value in values.items()},
        missing=[key for key in status['missing'] if key in FIELDS],
        invalid=[key for key in status['invalid'] if key in FIELDS],
        name_contains_mobile=bool(re.search(r'1[3-9]\d{9}',name)),
        name_contains_address=bool(address and len(address)>5 and address in name),
        name_has_source_evidence=bool(name and name_has_evidence(name,str(source or ''))),
        source=origin if origin in ('SOURCE_LOCAL','SOURCE_NICKNAME','SOURCE_AI','ADMIN_TEMPORARY') else 'UNSPECIFIED',
        generated=fields.get('_recipient_generated') is True,
        conflict_fields=[key for key in fields.get('_contact_source_conflicts',[]) if key in FIELDS]
            if isinstance(fields.get('_contact_source_conflicts',[]),list) else [])


def record_contact_evidence(stage, fields, source=''):
    """Non-critical, bounded, append-only0600 evidence independent of learning.

    No customer field values, fingerprints of individual phones/names, raw
    messages, model responses, keys, or provider exception strings are stored.
    Contention/limit/storage errors are explicit gaps, never order failures.
    """
    import fcntl,hashlib,json,logging,os,stat,time
    from pathlib import Path
    try:
        if stage not in ('ORDER_LOCAL','ORDER_PROVIDER','ORDER_FINAL','CONTACT_LOCAL','CONTACT_PROVIDER','CONTACT_FINAL'):
            raise ValueError('CONTACT_AUDIT_STAGE')
        from ai_diagnostics import CURRENT as DIAGNOSTIC
        from order_trace import CURRENT as TRACE
        from execution_safety import CURRENT as EXECUTION
        diagnostic=DIAGNOSTIC.get() or {};trace=TRACE.get() or {};job=(EXECUTION.get() or {}).get('job') or {}
        task=None
        if all(key in job for key in ('namespace','chat_id','message_id')):
            task=hashlib.sha256(('%s:%s:%s'%(job['namespace'],job['chat_id'],job['message_id'])).encode()).hexdigest()[:16]
        def safe_id(value):return value if isinstance(value,str) and re.fullmatch(r'[a-f0-9]{32}',value) else None
        row=dict(version=1,at=time.time(),stage=stage,task_id=task,
                 trace_id=safe_id(trace.get('trace_id')),request_id=safe_id(diagnostic.get('request_id')),
                 contact=contact_evidence_summary(fields,source))
        target=Path(os.getenv('RAINBOW_CONTACT_AUDIT_PATH') or Path(__file__).with_name('contact_recognition_audit.jsonl'))
        target.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(target,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>=8*1024*1024:raise ValueError('CONTACT_AUDIT_BOUND')
            os.fchmod(stream.fileno(),0o600)
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            stream.write(json.dumps(row,sort_keys=True,separators=(',',':'))+'\n')
            stream.flush()
        return True
    except Exception as error:
        logging.getLogger(__name__).warning('CONTACT_AUDIT_UNAVAILABLE kind=%s',type(error).__name__)
        return False

LABELS = {
    'recipient': ('收件人姓名', '收货人姓名', '收货人名字', '收货姓名', '收件姓名', '收货名', '收件名', '收货昵称', '收件人', '收货人', '姓名', '名字', '联系人', '昵称'),
    'phone': ('收件手机号', '收件手机', '收件电话', '手机号码', '手机号', '联系电话', '联系方式', '电话', '手机', '号码'),
    'address': ('收件地址', '收货地址', '配送地址', '所在地区', '详细地址', '地区', '地址'),
}
_LABEL_NAMES = '|'.join(sorted(sum((list(v) for v in LABELS.values()), []), key=len, reverse=True))
# Colonless labels are accepted only at a line boundary, not as arbitrary
# substrings in a recipient's name or delivery detail.
LABEL_RE = re.compile(r'(' + _LABEL_NAMES + r')\s*[:：]\s*|^[ \t]*[＊*]?(' + _LABEL_NAMES + r')[ \t]*(?=[^\n])', re.M)
CORRECTION_RE = re.compile(r'^(?:(?:打错了|上一个错了|这个为准|改成这个|地址改一下|正确的是|用这个|重新发)[\s，,:：。]*)+')

# Field labels, parser operation words and the actual production reply menu.
# These are syntax/control tokens, not recipient evidence, even when AI returns them.
NON_NAME_WORDS = frozenset(sum((list(v) for v in LABELS.values()), [])) | frozenset({
    '下单', '订单', '备注', '数量', '合计', '总计', '仓库', '商品', '货品',
    '物品', '购买', '口味', '品类', '型号', '取消', '确认', '确认下单',
    '入库', '出库', '批量入库', '批量出库', '单价查询', '库存预览',
    '购物车', '个人中心', '下单记录', '联系客服', '申请代发', '申请批发',
    '定制机器人', '下单模板', '帮助', '今日统计', '返回主菜单',
    '这个为准', '打错了', '重新发', '未知', '未提供',
    '号', '栋', '楼', '室', '单元',
    '京东快递', '顺丰快递', '中通快递', '圆通快递', '申通快递',
    '韵达快递', '邮政快递', '极兔快递', '德邦快递', '京东物流', '快递', '物流',
})

NAME_TOKEN = r'(?:[\u4e00-\u9fffA-Za-z]{1,6}|[A-Za-z][A-Za-z .·-]{1,19})(?:\[\d{1,12}\]|[（(]\d{1,12}[）)])?'
SEQUENCE_PREFIX = r'(?:(?:订单\s*)?\d{1,6}[.、:：]\s*|[①②③④⑤⑥⑦⑧⑨⑩]\s*)'

def strip_contact_sequence(value):
    return re.sub(r'^\s*'+SEQUENCE_PREFIX, '', str(value or '')).strip()

def strip_recipient_reference(value):
    """A labelled name's parenthesized customer code is not its name.

    Only a complete valid name + alphanumeric identifier can be split.
    Numeric name suffixes and arbitrary bracketed text remain unchanged.
    """
    value=unicodedata.normalize('NFKC',str(value or '')).strip()
    match=re.fullmatch(r'(.+?)\s*(?:[-—]\s*)?\([A-Za-z]{1,8}\d{3,12}\)',value)
    if match and valid_name(match.group(1).strip()):return match.group(1).strip()
    return value


def name_has_evidence(value, text):
    """Independent source boundary before applying shared name legality."""
    value = unicodedata.normalize('NFKC', str(value or '')).strip()
    labels = '|'.join(sorted(LABELS['recipient'], key=len, reverse=True))
    if re.fullmatch(r'\d{2,6}', value):
        return bool(re.search(r'(?:'+labels+r')\s*[:：]\s*'+re.escape(value)+r'(?=$|[\s,，;；|])',str(text)))
    prefix = r'(?:' + labels + r'|收件)(?:\s*[:：]?\s*|(?:的是|是|为|叫)\s*)'
    source='\n'.join(strip_contact_sequence(line) for line in unicodedata.normalize('NFKC', str(text or '')).splitlines())
    source=re.sub(r'(?:'+ '|'.join(sorted(LABELS['phone'],key=len,reverse=True)) +r')\s*[:：]?\s*(?=1[3-9]\d{9}(?!\d))',' ',source)
    # No separator is common between an institution's delivery name and the
    # person immediately before a complete mobile. This proves a *candidate*
    # span only; AI-only recovery still requires customer confirmation.
    if value and valid_name(value) and any(
            re.search(r'(?:宠物店|商店|门店|便利店|超市|公司|学校|医院|驿站|酒店|宾馆)'+re.escape(value)
                      +r'[ \t,，;；:：。.|]*1[3-9]\d{9}(?!\d)',line)
            and re.search(r'省|市|区|县',line) for line in source.splitlines()):
        return True
    for line in source.splitlines():
        labelled=re.fullmatch(r'(?:'+labels+r')\s*[:：]?\s*(.+)',line)
        if (labelled and strip_recipient_reference(labelled.group(1))==value
                and strip_recipient_reference(labelled.group(1))!=labelled.group(1).strip()):
            return True
    # Evidence must belong to a contact field, not merely occur somewhere in
    # the customer's goods line. AI cannot relabel a category/flavor as a name.
    usable=[]
    for line in source.splitlines():
        if (re.search(r'\d+\s*(?:支|根|件|个)|(?:×|x)\s*\d+',line)
                and not re.search(r'(?:'+labels+r')\s*[:：]?\s*'+re.escape(value)+r'(?=$|[\s,，;；|])',line)
                and not re.search(re.escape(value)+r'[ \t,，;；:：。.|]*1[3-9]\d{9}',line)):
            continue
        usable.append(line)
    source='\n'.join(usable)
    # A model-extracted name may directly follow a neighborhood/institution
    # without punctuation. The complete adjacent mobile plus administrative
    # address supplies contact-role evidence; no name whitelist is involved.
    if value and any(re.search(r'(?:^|[\s,，;；:：。.|号室层楼幢栋\d])'+re.escape(value)+r'[ \t,，;；:：。.|]*1[3-9]\d{9}(?!\d)',line)
                     and re.search(r'省|市|区|县',line) for line in usable):
        return True
    return bool(value) and bool(re.search(
        r'(?:^|[\s,，;；:：。.)）|]|(?<=\d)|' + prefix + r')' + re.escape(value)
        + r'(?=$|[\s,，;；。.|]|[:：][ \t]*(?:\+?86[ -]?)?1[3-9]\d{9}|1[3-9]\d{9})', source))


def valid_name(value):
    value = str(value or '').strip()
    return bool(re.fullmatch(NAME_TOKEN, value) or re.fullmatch(r'\d{2,6}',value)) and value.lower() not in {'xx', 'xxx', 'unknown', 'none', 'null', 'n/a'} and value not in NON_NAME_WORDS and not re.search(r'省|市|区|县|街道|路$|小区|单元|栋|室|(?:商店|宠物店|门店|便利店|公司|学校|医院|驿站|大厦|酒店|宾馆|中心)$', value)

def suspected_name(order):
    candidate=str(order.get('_suspected_recipient') or '').strip()
    source=str(order.get('_suspected_recipient_source') or '')
    return candidate if valid_name(candidate) and name_has_evidence(candidate,source) else ''

def explicit_name_in_source(value,source):
    labels='|'.join(sorted(LABELS['recipient'],key=len,reverse=True))
    return valid_name(value) and bool(re.search(r'(?:'+labels+r'|收件)(?:\s*[:：]?\s*|(?:的是|是|为|叫)\s*)'
        +re.escape(str(value))+r'(?=$|[\s,，;；。.|]|1[3-9]\d{9})',str(source),re.M))


def name_generation_allowed(order, status):
    """Explicit customer replacement is allowed for invalid names too."""
    missing = set(status.get('missing') or [])
    invalid = set(status.get('invalid') or [])
    # A fully validated merchandise draft must not depend on an optional
    # contact-AI absence flag to offer an explicit customer choice. This does
    # not fill a name: the persisted callback still requires authorization,
    # an owned draft and complete revalidation before any order is created.
    validated_items = bool(order.get('_validated_catalog_items') and order.get('items')
                           and not order.get('quantity_scope_errors')
                           and not order.get('requires_item_confirmation')
                           and not order.get('multiple_orders_detected'))
    from recipient_policy import temporary_name, requires_real_name
    if requires_real_name(order):
        return False
    return bool(suspected_name(order) or temporary_name(order)) and ((missing == {'recipient'} and not invalid and bool(order.get('_name_absent_confirmed') or suspected_name(order) or validated_items))
            or (invalid == {'recipient'} and not missing))


def _contact_role_view(text):
    """Split only a proven leading contact chain, not words inside an address.

    Colonless inline labels previously consumed the rest of the contact row
    as a name. Numeric phone syntax proves the boundary even for an invalid
    number; validation must still reject it rather than repairing its digits.
    """
    recipients = '|'.join(sorted(LABELS['recipient'], key=len, reverse=True))
    phones = '|'.join(sorted(LABELS['phone'], key=len, reverse=True))
    addresses = '|'.join(sorted(LABELS['address'], key=len, reverse=True))
    name_span = NAME_TOKEN.replace('{1,6}', '{1,6}?').replace('{1,19}', '{1,19}?')
    from address_utils import PROVINCE_ALIASES, MUNICIPALITIES
    province_prefix='|'.join(re.escape(value) for value in sorted(
        set(PROVINCE_ALIASES)|set(PROVINCE_ALIASES.values())|set(MUNICIPALITIES),key=len,reverse=True))
    result = []
    for line in str(text).splitlines():
        # A complete leading name/mobile followed immediately by an explicit
        # province is contact syntax, including all trailing building digits.
        inline_address=re.match(r'^\s*(?:(?:'+recipients+r')\s*[:：]?\s*)?('+NAME_TOKEN+r')[ \t,，;；|]*((?:\+86[ -]?)?1[3-9]\d{9})'
                                r'[ \t,，;；|]*(?=(?:'+province_prefix+r'))',line)
        if inline_address:
            line='收件人：'+inline_address.group(1)+'\n电话：'+inline_address.group(2)+'\n地址：'+line[inline_address.end():]
        # A complete leading mobile proves an inline field boundary even
        # without a colon: "19999990099收件人peachy". Keep the label so
        # normal name validation, rather than a guessed name, stays decisive.
        leading_mobile=re.match(r'^(\s*(?:\+86[ -]?)?1[3-9]\d{9})[ \t,，;；|]*(?=(?:'+recipients+r')\s*[:：]?\s*\S)',line)
        if leading_mobile:
            line=line[:leading_mobile.end(1)]+'\n'+line[leading_mobile.end():]
        match = re.match(r'^(\s*[＊*]?(?:'+recipients+r')\s*[:：]?\s*)('
                         +name_span+r')([ \t,，;；|]*)(?=(?:'+phones+r')\s*[:：]?\s*\+?\d)', line)
        if match and valid_name(match.group(2).strip()):
            line = line[:match.end(2)]+'\n'+line[match.end():]
        chunks = []
        for part in line.splitlines():
            # A labelled numeric field or a leading name+complete mobile is
            # evidence of a phone/address role transition. A shop name is not.
            match = re.match(r'^(?:\s*(?:'+phones+r')\s*[:：]?\s*\+?[\d -]+|'
                             r'\s*(?:(?:'+recipients+r')\s*[:：]?\s*)?'+NAME_TOKEN+
                             r'[ \t,，;；:：|]*(?:\+86[ -]?)?1[3-9]\d{9})[ \t,，;；|]*'
                             r'(?=(?:'+addresses+r')\s*[:：]?\s*\S)', part)
            if match:
                part = part[:match.end()].rstrip(' ,，;；|')+'\n'+part[match.end():]
            chunks.append(part)
        result.extend(chunks)
    return '\n'.join(result)


def deterministic_name_absence(text, fields, products=()):
    """Prove a missing name only when every remaining line is merchandise.

    This is the provider-outage fallback for the explicit name-generation
    button.  It deliberately requires a live catalog plus already validated
    phone/address fields; any unexplained prose or possible person token keeps
    the order pending without authorising a generated name.
    """
    if (not products or fields.get('recipient') or not fields.get('phone')
            or not fields.get('address')):
        return False
    from order_structure import catalog_names
    names = {name.casefold() for name in catalog_names(products)}
    if not names:
        return False
    from order_structure import structure_order_text
    residual = structure_order_text(merchandise_text(text, fields=fields, products=products),products=products).text
    saw_item = False
    for raw_line in residual.splitlines():
        line = re.sub(LABEL_RE, '', raw_line).strip(' \t,，;；:：。|')
        if not line:
            continue
        normalized = unicodedata.normalize('NFKC', line).strip()
        normalized=re.sub(r'^(?:品类|型号)\s*[:：]\s*','',normalized)
        if re.fullmatch(r'\d{1,6}',normalized) and re.search(
                r'(?:^|\n)\s*'+re.escape(normalized)+r'[ \t]+(?:\+86[ -]?)?'+re.escape(str(fields['phone']))+r'\s*(?:\n|$)',str(text)):
            continue
        if re.fullmatch(r'(?:共计|共|合计|总计|总共|一共)\s*\d+\s*(?:根|支|件|个|条)',normalized):continue
        if normalized in {'下单','订单','购买','下单模板','商品','货品','物品','产品'}:
            continue
        normalized = re.sub(r'^(?:商品|产品|购买|货品|物品)\s*[:：;；]\s*','',normalized)
        if normalized.casefold() in names:
            continue
        # A standalone price/subtotal equation is not a recipient candidate
        # and must never be interpreted as an item quantity.  It may be
        # ignored only when the whole line is arithmetic; prose or a bare
        # number still keeps the draft pending.
        if re.fullmatch(
            r'[¥￥]?\s*\d+(?:\.\d+)?(?:\s*[x×*+\-/]\s*\d+(?:\.\d+)?)+'
            r'(?:\s*=\s*[¥￥]?\d+(?:\.\d+)?)?\s*(?:元)?',
            normalized,
        ):
            continue
        parts = [part.strip() for part in re.split(r'[,，;；]+', normalized) if part.strip()]
        if len(parts) > 1:
            bound = [re.fullmatch(r'(.+?)(?:[x×*])?(\d+)\s*(?:支|根|件|个|条)?', part)
                     for part in parts]
            if all(match and int(match.group(2)) > 0
                   and match.group(1).strip().casefold() in names for match in bound):
                saw_item = True
                continue
        item = re.fullmatch(r'(.+?)(?:[x×*])?(\d+)\s*(?:支|根|件|个|条)?', normalized)
        if not item or int(item.group(2)) <= 0 or item.group(1).strip().casefold() not in names:
            return False
        saw_item = True
    return saw_item


def contact_catalog_names(products=()):
    """Live merchandise identities are not evidence of a bare recipient."""
    from rule_dictionary import runtime_store
    from brand_aliases import scope_matches
    pool = [p for p in products if p.get('is_active', True) and not p.get('is_archived')]
    if not pool:
        return set()
    by_category = {}
    for product in pool:
        by_category.setdefault(product.get('parent_name'), []).append(product)
    names = {str(p.get('parent_name') or '').casefold() for p in pool}
    for row in runtime_store().get('aliases', []):
        if (row.get('type') == 'MODEL_ALIASES' and row.get('status') == 'active'
                and any(scope_matches(row, p) for p in by_category.get(row.get('standard'), ()))):
            names.add(str(row.get('alias') or '').casefold())
    return names - {''}


def catalog_goods_line(value,products=(),headings=None):
    """A complete reviewed list is goods even if its model contains digits.

    In particular ICE12K + flavor1 is not a building-wing continuation.
    Names inside a place (冰爆大厦3号楼) do not satisfy this whole-line proof.
    """
    text=str(value).strip()
    if headings is None:headings=contact_catalog_names(products)
    from catalog_regex import compiled
    hits=[n for n in headings if compiled(re.escape(n)+r'(?=$|[\s:：])',re.I).match(text)]
    if not hits:return False
    from category_evidence import prepare
    from rule_dictionary import runtime_store
    from order_structure import catalog_names
    rows=runtime_store().get('aliases',[])
    heading=max(hits,key=len)
    targets={p.get('parent_name') for p in products if str(p.get('parent_name') or '').casefold()==heading}
    targets.update(r.get('standard') for r in rows if r.get('type')=='MODEL_ALIASES'
                   and r.get('status')=='active' and str(r.get('alias') or '').casefold()==heading)
    if len(targets)!=1:return False
    pool=[p for p in products if p.get('parent_name') in targets and p.get('is_active',True) and not p.get('is_archived')]
    names={n.casefold() for n in catalog_names(pool,rows)}
    prepared=prepare(text,pool,rows)
    items=[]
    for line in prepared.splitlines():
        if re.match(r'^品类[:：]',line) or line.strip().casefold() in headings:continue
        match=re.fullmatch(r'(.+?)[×xX*](\d+)',line.strip())
        if not match or int(match.group(2))<=0 or match.group(1).strip().casefold() not in names:return False
        items.append(match)
    return bool(items)


def split_inline_catalog_goods(text, products=()):
    """Split only a complete catalog-proven trailing item/list after contact text."""
    from order_structure import strip_category_enumeration
    text=strip_category_enumeration(text,products)
    from deepseek_resolver import resolve_flavor
    from rule_dictionary import runtime_store
    from brand_aliases import scope_matches
    live=[p for p in products if p.get('parent_name') and p.get('name') and p.get('is_active',True) and not p.get('is_archived')]
    # Invocation-local index: avoids scanning the entire catalog per alias and
    # source line, without caching changing inventory, prices, or approvals.
    by_category={}
    for product in live:
        by_category.setdefault(product['parent_name'],[]).append(product)
    mappings={p['parent_name']:{p['parent_name']} for p in live}
    rows=runtime_store().get('aliases',[]) if live else []
    for row in rows:
        if row.get('type')=='MODEL_ALIASES' and row.get('status')=='active' and any(scope_matches(row,p) for p in by_category.get(row.get('standard'),())):
            mappings.setdefault(row['alias'],set()).add(row['standard'])
    # Explicit category + flavor field labels prove a merchandise boundary,
    # even when both fields share the address line. Keep the unknown flavor
    # body intact for catalog validation instead of absorbing it as address.
    from category_evidence import normalize_heading_specs,packaging_aliases
    mappings=packaging_aliases(mappings)
    normalized=normalize_heading_specs(text,mappings)
    normalized=re.sub(r'[\u200b\u200c\ufeff]','',normalized)
    # Explicit field labels prove roles even for an unknown product. Split
    # both directions before removing merchandise from the contact view;
    # otherwise "商品:... 地址:..." can lose its address along with goods.
    goods_label=r'(?:下单产品|下单商品|商品|产品|货品|物品|购买)\s*[:：;；]'
    if re.search(goods_label,normalized):
        role_label=r'(?:下单产品|下单商品|商品|产品|货品|物品|购买|详细地址|收货地址|地址|收件人|收货人|姓名|手机号|手机号码|电话)\s*[:：;；]'
        # Non-overlapping matches avoid splitting 收货地址 into 收货 + 地址,
        # or 下单产品 into 下单 + 产品 when the long label starts the line.
        cuts=[m.start() for m in re.finditer(role_label,normalized) if m.start() and normalized[m.start()-1]!='\n']
        for cut in reversed(cuts):normalized=normalized[:cut]+'\n'+normalized[cut:]
        normalized=re.sub(goods_label,lambda m:'下单产品：' if m.group().startswith('下单') else '商品：',normalized)
    source_lines=[]
    for line in normalized.splitlines():
        fields=list(re.finditer(r'品类\s*[:：]\s*([^:：。\n]+?)\s*[。.]*\s*口味\s*[:：]',line))
        if len(fields)==1:
            field=fields[0];alias=field.group(1).strip().rstrip('.').strip()
            targets=set().union(*(v for k,v in mappings.items() if k.casefold()==alias.casefold()))
            if len(targets)==1:
                if line[:field.start()].strip():source_lines.append(line[:field.start()].rstrip())
                source_lines.append(next(iter(targets))+'：'+line[field.end():]);continue
        source_lines.append(line)
    output=[]
    has_admin_context=bool(re.search(r'省|市|区|县',normalized))
    for line_index,line in enumerate(source_lines):
        matches=[]
        for alias,targets in mappings.items():
            if len(targets)!=1:continue
            category=next(iter(targets));pool=by_category[category]
            for match in compiled(re.escape(alias),re.I).finditer(line):
                prefix=line[:match.start()]
                # Decoration belongs to merchandise, not to the address.
                prefix=re.sub(r'(?<=\s)正品\s*$','',prefix)
                if not re.search(r'省|市|区|县',prefix) and not (
                        has_admin_context and re.match(r'^\s*详细地址\s*[:：]',prefix)):
                    continue
                # Compact merchandise may immediately follow a room number.
                # A Chinese place-name substring is not such a boundary.
                explicit_detail=(has_admin_context and re.match(r'^\s*详细地址\s*[:：]',prefix)
                                 and re.search(r'\d+.*(?:栋|幢|楼|室|公司|科技|大厦|小区)',prefix))
                if not prefix or not (re.search(r'(?:[\s，,;；\d号室房楼层]|驿站|快递柜|快递站)$',prefix) or explicit_detail):continue
                tail=line[match.end():].strip(' :：')
                following_heading=(not tail and re.fullmatch(r'\s*[:：]\s*',line[match.end():])
                                   and line_index+1<len(source_lines))
                validation_tail=source_lines[line_index+1].strip() if following_heading else tail
                known={p['name'].casefold() for p in pool}
                known.update(str(r.get('alias') or '').casefold() for r in rows if r.get('type')=='EXPLICIT_FLAVOR' and r.get('status')=='active' and r.get('category')==category and any(p['name']==r.get('standard') and scope_matches(r,p) for p in pool))
                # Contact separation is not quantity interpretation: prove the
                # entire goods list against the live catalog, retaining its
                # subtotal and any conflict for the normal Validator. Never
                # call the full order parser here (it calls parse_contact).
                from order_structure import fulfillment_parts, catalog_names
                directive=fulfillment_parts(validation_tail,catalog_names(products,rows))
                primary=directive[0] if directive else validation_tail
                body=re.split(r'备选口味\s*[:：]',primary,maxsplit=1)[0].strip().rstrip('。.')
                if directive:body=body.rstrip('，,')
                wrapped=re.fullmatch(r'[{｛]\s*([^{}｛｝]+)\s*[}｝]\s*(?:[xX×*]\s*)?(\d+)\s*(?:支|根|个|件)?',body)
                if wrapped and wrapped.group(1).strip().casefold() in known:
                    body=wrapped.group(1).strip()+'×'+wrapped.group(2)
                compact_total=re.match(r'^[xX×*]\s*(\d+)\s*',body)
                if compact_total:
                    body=body[compact_total.end():]
                declared=re.match(r'^(\d+)\s*(?:支|只|根|件|个)\s*[:：]\s*',body)
                body=re.sub(r'^\d+\s*(?:支|只|根|件|个)\s*[:：]\s*','',body)
                labels='|'.join(re.escape(n) for n in sorted(known,key=lambda n:(-len(n),n)))
                token=re.compile(r'('+labels+r')\s*(?:(?:[xX×*]\s*)?(\d+)(?:\s*(?:支|只|根|件|个))?)?',re.I) if labels else None
                pos=0;count=0;total=0;valid=True;proven=[]
                while pos<len(body):
                    item=token.match(body,pos) if token else None
                    if not item or int(item.group(2) or 1)<=0:
                        valid=False;break
                    flavor,_=resolve_flavor(category,item.group(1),[p['name'] for p in pool],products=pool)
                    if not flavor:valid=False;break
                    count+=1;total+=int(item.group(2) or 1);pos=item.end()
                    proven.append(item.group(1)+'×'+str(int(item.group(2) or 1)))
                    if pos==len(body):break
                    sep=re.match(r'[\s,，、;；]+',body[pos:])
                    if sep:pos+=sep.end()
                    elif not (token and token.match(body,pos) and (compact_total or item.group(2) is not None)):
                        valid=False;break
                    if pos==len(body):valid=False;break
                if compact_total and total!=int(compact_total.group(1)):valid=False
                if not valid and declared:
                    # A catalog-proven model plus a conserved explicit list
                    # proves the *boundary*, not the identity of its flavors.
                    # Preserve every raw flavor for normal matching/rejection;
                    # this must never silently discard an unknown item.
                    generic=re.compile(r'([^,，、;；:：\d]+?)\s*(?:[xX×*]\s*)?(\d+)(?:\s*(?:支|只|根|件|个))?')
                    cursor=0;quantities=[];valid=True
                    while cursor<len(body):
                        item=generic.match(body,cursor)
                        if (not item or int(item.group(2))<=0
                                or re.search(r'地址|电话|收件|收货|楼|栋|室|路|街|小区|公寓|学校',item.group(1))):
                            valid=False;break
                        quantities.append(int(item.group(2)));cursor=item.end()
                        if cursor==len(body):break
                        sep=re.match(r'[\s,，、;；]+',body[cursor:])
                        if not sep:valid=False;break
                        cursor+=sep.end()
                        if cursor==len(body):valid=False;break
                    valid=valid and len(quantities)>=2 and sum(quantities)==int(declared.group(1))
                    count=len(quantities)
                if valid and count:
                    goods=('\n'.join(proven)+('\n共'+str(total)+'支' if compact_total else '')) if compact_total or wrapped else tail
                    matches.append((match.start(),len(alias),prefix.rstrip(),alias+'：'+goods,category))
        if matches:
            first=min(m[0] for m in matches)
            chosen=max((m for m in matches if m[0]==first),key=lambda m:m[1])
            # Audited aliases may nest (柚子独角兽 / 独角兽). They prove
            # one boundary only when every hit lies inside the longest alias
            # and points to the same exact category, never by similarity.
            if all(m[4]==chosen[4] and chosen[0]<=m[0] and m[0]+m[1]<=chosen[0]+chosen[1] for m in matches):
                output.extend((chosen[2],chosen[3]));continue
        output.append(line)
    return '\n'.join(output)

def _contact_cache_key(text, waiting=None, products=()):
    from category_evidence import _prepare_cache_key
    from rule_dictionary import runtime_store
    return (waiting, _prepare_cache_key(text,products,runtime_store().get('aliases',[])))


from request_memo import request_memoized


@request_memoized(_contact_cache_key)
def parse_contact(text, waiting=None, products=()):
    from order_parser import OrderParser

    text = split_inline_catalog_goods(CORRECTION_RE.sub('', str(text or '').strip()),products)
    text = '\n'.join(strip_contact_sequence(line) for line in text.splitlines())
    # Normalize only complete mobile-number spans, not address/model numbers.
    text = re.sub(r'(?<!\d)(?:\+86[ -]?)?(1[3-9]\d[ -]?\d{4}[ -]?\d{4})(?!\d)',
                  lambda m: re.sub(r'[ -]', '', m.group(1)), text)
    from order_structure import structure_order_text
    original_contact_text = unicodedata.normalize('NFKC', text)
    # Keep raw customer evidence unchanged; only the parser's role view gains
    # boundaries. Source/address verification below uses the same proven view.
    text = _contact_role_view(text)
    original_contact_text = _contact_role_view(original_contact_text)
    text = structure_order_text(text).text
    text = OrderParser._strip_embedded_items_for_delivery(OrderParser._normalize_text(text))
    text = re.sub(r'(?:商品|产品|货品|物品|购买)\s*[:：;；][^\n]*', '', text)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    fields = {}
    # The generic structure adapter can split a comma-separated contact row
    # into "收货人\n姓名". Retain the explicitly labelled source field before
    # that transformation, but never infer or reconstruct a missing name.
    source_name=re.search(r'(?:'+ '|'.join(sorted(LABELS['recipient'],key=len,reverse=True))
        +r')\s*[:：]\s*('+NAME_TOKEN+r')(?=$|[\n,，;；])',original_contact_text)
    if source_name and valid_name(source_name.group(1).strip()):
        fields['recipient']=source_name.group(1).strip()
    matches = list(LABEL_RE.finditer(text))
    address_parts = []
    nickname = None
    explicit_name = None
    for i, match in enumerate(matches):
        label = match.group(1) or match.group(2)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[match.end():end].strip(' \t\n,，;；|')
        field = next(k for k, labels in LABELS.items() if label in labels)
        # Keep source-proven multiline delivery continuation (including a
        # town split across lines), stopping before catalog/quantity syntax.
        value_lines=value.splitlines()
        value=value_lines[0].strip() if value_lines else ''
        if field=='address':
            goods=contact_catalog_names(products)
            for continuation in value_lines[1:]:
                continuation=continuation.strip()
                if not continuation:continue
                if (any(continuation.casefold().startswith(n) for n in goods)
                        or re.search(r'1[3-9]\d{9}|[xX×*]\s*\d+|\d+\s*(?:支|根|件|个)|备选|备用',continuation)):
                    break
                if not re.search(r'镇|乡|路|街|巷|号|楼|栋|幢|单元|室|房|银行|学校|学院|小区|园|舍|快递|驿站',continuation):break
                value+=continuation
        if field == 'address':
            if label == '详细地址':
                address_parts.append(value)
            else:
                address_parts = [value]
        else:
            fields[field] = strip_recipient_reference(value) if field=='recipient' else value
            if label in ('收货昵称','昵称') and valid_name(fields[field]):
                nickname=fields[field]
            elif field=='recipient' and valid_name(fields[field]):
                explicit_name=fields[field]
    if explicit_name:
        # Latest administrator requirement: explicit source name precedes a
        # labelled nickname; both remain above AI and configured fallback.
        fields['recipient']=explicit_name
        fields['_recipient_source']='SOURCE_LOCAL'
    elif nickname:
        fields['recipient']=nickname
        fields['_recipient_source']='SOURCE_NICKNAME'
    # A labelled recipient may contain a phone on the same line. Split only
    # a complete valid-name + phone pair; never guess names from addresses.
    recipient_pair = re.fullmatch(
        r'('+NAME_TOKEN+r')[\s,，;；:：。.|]*(?:\+?86[ -]?)?(1[3-9]\d{9})[\s,，;；。.|]*',
        fields.get('recipient', ''),
    )
    if recipient_pair and valid_name(recipient_pair.group(1)):
        fields['recipient'] = recipient_pair.group(1).strip()
        fields.setdefault('phone', recipient_pair.group(2))
    if address_parts:
        # A standalone administrative line preceding 详细地址 belongs to
        # the same contact address; the label must not discard that region.
        if matches and (matches[0].group(1) or matches[0].group(2)) == '详细地址':
            prefix_lines = text[:matches[0].start()].splitlines()
            regions = [line.strip() for line in prefix_lines
                       if re.search(r'省|市|区|县|街道', line)
                       and not re.search(r'1[3-9]\d{9}|\d+\s*(?:支|根|件|个)', line)]
            address_parts = regions + address_parts
        fields['address'] = ''.join(address_parts)
    address, name, phone, _ = OrderParser._extract_flexible_delivery(lines, text)
    if 'phone' not in fields and phone:
        fields['phone'] = phone
    if not fields.get('phone'):
        # Merchandise adapters can consume a bare final numeric line. Recover
        # only a complete source contact line, never digits embedded in goods,
        # an address, an oversized number, or multiple competing mobiles.
        source_mobiles=[]
        for source_line in original_contact_text.splitlines():
            match=re.fullmatch(r'\s*(?:'+NAME_TOKEN+r'\s*)?(?:\+86[ -]?)?(1[3-9]\d{9})\s*',source_line)
            if match:source_mobiles.append(match.group(1))
        if len(set(source_mobiles))==1:
            fields['phone']=source_mobiles[0]
    if 'address' not in fields and address:
        fields['address'] = address
    # A recipient separated from a labelled mobile by punctuation is still
    # a strong contact-role span. Resolve it before falling back to a bare
    # line, which could be a category heading rather than a person.
    labelled_pair = re.search(
        r'(?:^|[,，;；])\s*('+NAME_TOKEN+r')\s*[,，;；]\s*(?:'
        + '|'.join(sorted(LABELS['phone'], key=len, reverse=True))
        + r')\s*[:：]\s*1[3-9]\d{9}(?!\d)', original_contact_text, re.M)
    if 'recipient' not in fields and labelled_pair and valid_name(labelled_pair.group(1)):
        fields['recipient'] = labelled_pair.group(1).strip()
    name_source=re.sub(r'(?:'+ '|'.join(sorted(LABELS['phone'],key=len,reverse=True)) +r')\s*[:：]?\s*(?=1[3-9]\d{9}(?!\d))',' ',original_contact_text)
    near_phone = re.search(r'(?:^|[\s,，;；。.)）]|(?<=\d))('+NAME_TOKEN+r')[\s,，;；:：。.|]*(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)', name_source)
    if ('recipient' not in fields and near_phone and valid_name(near_phone.group(1))
            and not re.match(r'^(?:号|楼|栋|幢|室|单元)',near_phone.group(1))):
        fields['recipient'] = near_phone.group(1).strip()
    goods_names = contact_catalog_names(products)
    if 'recipient' not in fields:
        source_lines = original_contact_text.splitlines()
        for i, line in enumerate(source_lines):
            if re.fullmatch(r'\s*(?:\+?86)?1[3-9]\d{9}\s*', line) and i:
                candidate = source_lines[i-1].strip()
                if valid_name(candidate) and candidate.casefold() not in goods_names:
                    fields['recipient'] = candidate
                    break
    # Prefer exact customer address spans over an external parser's rewritten
    # address. Contact roles must not leak a one-character name into province.
    # Recovery still uses customer spans, but auxiliary warehouse/amount
    # roles already consumed by the shared structure gate cannot be reused
    # as delivery continuation (e.g. an alphabetic floor-like note).
    source_lines = structure_order_text(original_contact_text).text.splitlines()
    for line_index, source_line in enumerate(source_lines):
        probe = re.sub(r'^(?:'+ '|'.join(sorted(LABELS['address'],key=len,reverse=True)) +r')\s*[:：]?\s*', '', source_line).strip()
        # Order-control words preceding an address are not address prefixes.
        # Starting continuation recovery from them duplicated the proven
        # administrative chain (for example “下单广东省…”).
        if probe in NON_NAME_WORDS:
            continue
        if re.search(r'1[3-9]\d{9}', probe):
            if fields.get('recipient'):
                probe = re.sub(re.escape(fields['recipient'])+r'[\s,，;；:：。.|]*(?=1[3-9]\d{9})',' ',probe)
            before, after = re.split(r'1[3-9]\d{9}', probe, maxsplit=1)
            probe = (before if re.search(r'省|市|区|县|路|街|楼|栋',before) else after).strip(' ,，:：。')
            # A phone field label immediately before the mobile belongs to
            # the contact syntax, never to the delivery detail.
            probe = re.sub(r'(?:'+ '|'.join(sorted(LABELS['phone'],key=len,reverse=True)) +r')\s*[:：]?\s*$', '', probe).strip()
        # A merchandise heading must not gain delivery identity merely by
        # concatenating a later address line. Require address-role evidence in
        # the starting source span before considering detail continuation.
        if not re.search(r'省|市|区|县|街道|镇|乡|路|街|号|楼|栋|幢|室|花园|小区|公寓|学院|大学|学校|驿站',probe):
            continue
        for continuation in source_lines[line_index+1:]:
            if catalog_goods_line(continuation,products,goods_names):break
            # Structure parsing separates a model heading from its item list.
            # A live catalog heading is merchandise even if the next flavor
            # is unresolved and its quantity uses a bare digit (live883).
            if continuation.strip().casefold() in goods_names:
                break
            from catalog_regex import compiled
            if (any(compiled(re.escape(token)+r'(?=$|[\s:：])',re.I).match(continuation) for token in goods_names)
                    and re.search(r'[xX×*]\s*\d+|\d+\s*(?:支|根|个|件)',continuation)):
                break
            if (not re.search(r'学院|大学|学校|中学|小学|高中|幼儿园|驿站|公寓|花园|小区|栋|单元|室|商场|俱乐部|酒店|店|银行|镇|乡|路|街|巷|\d+号|[A-Za-z]\d',continuation)
                    or re.search(r'1[3-9]\d{9}|[xX×*]\s*\d+|\d+\s*(支|根|个|件)|备选|备用',continuation)):
                break
            probe += re.sub(r'^(?:'+ '|'.join(sorted(LABELS['address'],key=len,reverse=True)) +r')\s*[:：]?\s*','',continuation).strip()
        probe = re.sub(r'(?:详细地址|收件地址|收货地址)\s*[:：]\s*', '', probe)
        # Administrative fuzzy matches inside a flavor name are not proof
        # that this source line is an address. Require a delivery-role cue
        # before accepting the administrative normalizer's inferred chain.
        if not re.search(r'省|市|区|县|街道|镇|乡|路|街|号|楼|栋|幢|室|花园|小区|公寓|学院|大学|学校|驿站', probe):
            continue
        meta = normalize_cn_address(probe)
        if all(meta.get(k) for k in ('province','city','detail')) and (meta.get('district') or meta.get('town')):
            # Continuation recovery must not replace an already-labelled
            # region/detail address with a detail-only inferred chain.
            # E.g. 八里庄西里 is a Beijing delivery span, not evidence that
            # an explicitly Beijing/朝阳 address belongs to Taiwan 八里区.
            existing = normalize_cn_address(fields.get('address') or '')
            if any(existing.get(key) and meta.get(key)
                   and existing[key] != meta[key]
                   for key in ('province', 'city', 'district')):
                continue
            fields['address'] = probe
            break
    after_phone = re.search(r'(?<!\d)1[3-9]\d{9}[ \t,，;；|]*([A-Za-z][A-Za-z.·-]{0,19}|[\u4e00-\u9fff]{1,6})(?=$|[\s,，;；|])', original_contact_text)
    if 'recipient' not in fields and after_phone and valid_name(after_phone.group(1)):
        fields['recipient'] = after_phone.group(1)
    # Accept inferred names only with an independent text boundary, never a
    # substring guessed from a street/address by the external address parser.
    if 'recipient' not in fields and name and valid_name(name) and name.casefold() not in goods_names:
        # The delivery view removes goods syntax. Its remaining brand heading
        # is not independent evidence of a recipient in the customer original.
        if name_has_evidence(name, original_contact_text):
            fields['recipient'] = name
    if waiting == 'recipient' and not matches and text and not fields:
        fields['recipient'] = text
    if waiting == 'phone' and not matches and text and not fields:
        fields['phone'] = text
    if waiting == 'address' and 'address' not in fields and text and not matches:
        fields['address'] = text
    if 'address' not in fields:
        # Preserve concrete delivery detail even when its administrative
        # region cannot be resolved. It must become Pending, not disappear.
        candidates=[line.strip() for line in original_contact_text.splitlines()
                    if re.search(r'学院|大学|学校|小区|花园|公寓|\d+号楼|\d+栋',line)
                    and not re.search(r'1[3-9]\d{9}|\d+\s*(支|根|个|件)|备选|备用',line)]
        if len(candidates)==1:fields['address']=candidates[0]
    if 'phone' in fields:
        fields['phone'] = re.sub(r'[\s-]', '', fields['phone'])
        if fields['phone'].startswith('+86'):
            fields['phone'] = fields['phone'][3:]
    if fields.get('address'):
        # Retain explicitly labelled room/building detail as delivery text,
        # even though merchandise quantity parsing masks the same numbers.
        from numeric_metadata import DETAIL_LABEL,DETAIL_VALUE
        for detail in re.finditer(r'(?m)^\s*('+DETAIL_LABEL+r')\s*[:：]\s*('+DETAIL_VALUE+r')\s*$',original_contact_text):
            if detail.group(2) not in str(fields['address']):
                fields['address']=str(fields['address']).rstrip()+' '+detail.group(1)+detail.group(2)
        # Remove repeated municipality province/city level, then use the
        # existing administrative tree and direct-admin-city support.
        address = re.sub(r'\s+', '', fields['address'])
        address = re.sub(r'^(北京市|上海市|天津市|重庆市)\1', r'\1', address)
        fields['raw_address'] = address
        fields['address'] = normalize_cn_address(address)['normalized_address']
    if fields.get('recipient') and name_has_evidence(fields['recipient'],original_contact_text):
        fields.setdefault('_recipient_source','SOURCE_LOCAL')
    return fields


def with_generated_name(fields):
    """Administrator-authorized default for every empty recipient field.

    Other missing/invalid fields remain independently invalid. Never replace
    an existing name or claim this temporary label came from source text.
    """
    result=dict(fields)
    if valid_name(result.get('recipient')):return result
    from recipient_policy import temporary_name
    label=temporary_name(result)
    if not label:
        return result
    result.update(recipient=label,_recipient_generated=True,
                  _recipient_source='ADMIN_TEMPORARY',_recipient_learning_excluded=True)
    result['contact_diagnostics']=diagnose_contact(result)
    return result


def merchandise_text(text, fields=None, contact_labels=False, products=()):
    """Mask only source-proven recipient/mobile spans, retaining line offsets.

    Raw text remains the evidence source. This view is for merchandise parsing
    only, so a resolved contact token cannot be consumed as a category.
    """
    text=split_inline_catalog_goods(text,products)
    if contact_labels:
        # The goods parser must see the same proven contact-role boundaries
        # as parse_contact. Otherwise its legacy delivery fallback can steal
        # a following short category heading as the recipient and lose scope.
        # Only the parser view changes; original evidence and the unlabelled
        # masking view retain their text/line offsets.
        text=_contact_role_view(text)
    fields = parse_contact(text, products=products) if fields is None else fields
    name = str(fields.get('recipient') or '').strip()
    if not name or not name_has_evidence(name, text):name=''
    # Address extraction joins source lines. Match that same proven character
    # sequence across newlines before examining individual merchandise lines.
    # Never infer an address merely from its numbers or its position.
    source_lines = str(text).splitlines()
    normalized_lines = [unicodedata.normalize('NFKC', line) for line in source_lines]
    address_token = unicodedata.normalize('NFKC', str(fields.get('raw_address') or '').strip())
    address_pattern = r'\s*'.join(re.escape(c) for c in address_token) if address_token else None
    joined = '\n'.join(normalized_lines)
    full_address = re.search(address_pattern, joined) if address_pattern else None
    multiline_address_lines = set()
    if full_address and '\n' in full_address.group():
        offset = 0
        for index, normalized_line in enumerate(normalized_lines):
            start = max(offset, full_address.start())
            end = min(offset + len(normalized_line), full_address.end())
            if start < end:
                residual = normalized_line[:start-offset] + normalized_line[end-offset:]
                residual = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '', residual)
                if name:
                    residual = residual.replace(unicodedata.normalize('NFKC', name), '')
                residual = re.sub(LABEL_RE, '', residual).strip(' ,，;；:：。|')
                if not residual:
                    multiline_address_lines.add(index)
            offset += len(normalized_line) + 1
    result=[]
    for index, line in enumerate(source_lines):
        if index in multiline_address_lines:
            result.append('地址：' + line if contact_labels else ' ' * len(line))
            continue
        candidate=strip_contact_sequence(line)
        raw_address = str(fields.get('raw_address') or '').strip()
        # Contact address can share a line with a name/mobile and punctuation.
        # Match only the proven original address characters, allowing source
        # whitespace; never treat merely similar text as an address span.
        normalized_line = unicodedata.normalize('NFKC', line)
        address_token = unicodedata.normalize('NFKC', raw_address)
        address_pattern = r'\s*'.join(re.escape(c) for c in address_token) if address_token else None
        address_match = re.search(address_pattern, normalized_line) if address_pattern else None
        if address_match:
            residual = normalized_line[:address_match.start()] + normalized_line[address_match.end():]
            residual = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '', residual)
            if name:
                residual = residual.replace(unicodedata.normalize('NFKC', name), '')
            residual = re.sub(LABEL_RE, '', residual).strip(' ,，;；:：。|')
            if not residual:
                if contact_labels:
                    contact_block=['地址：'+raw_address]
                    if name and unicodedata.normalize('NFKC',name) in normalized_line:
                        contact_block.append('收件人：'+name)
                    mobile_in_line=re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)',normalized_line)
                    if mobile_in_line:
                        contact_block.append('电话：'+mobile_in_line.group())
                    result.append('\n'.join(contact_block))
                else:
                    result.append(' ' * len(line))
                continue
        if raw_address and line.strip() == raw_address:
            result.append('地址：' + raw_address if contact_labels else ' ' * len(line))
            continue
        if name and candidate==name:
            result.append('收件人：'+name if contact_labels else ' '*len(line));continue
        # A proven labelled recipient is contact data even without a mobile
        # on that same line. Do not leave its name/label in AI goods evidence.
        recipient_label=re.match(r'^[ \t]*[＊*]?(?:'+ '|'.join(sorted(LABELS['recipient'],key=len,reverse=True)) +r')\s*[:：]?\s*(.*)$',line)
        if recipient_label and name and recipient_label.group(1).strip()==name:
            result.append('收件人：'+name if contact_labels else ' '*len(line));continue
        if contact_labels:
            result.append(line);continue
        masked=line
        if name:
            pattern=re.escape(name)+r'(?=[\s,，;；:：|]*(?:\+?86[ -]?)?1[3-9]\d[ -]?\d{4}[ -]?\d{4})'
            masked=re.sub(pattern,lambda m:' '*len(m.group()),masked)
        probe=re.sub(r'^(?:'+ '|'.join(LABELS['address']) +r')\s*[:：]\s*', '', line).strip()
        mobile=re.search(r'1[3-9]\d[ -]?\d{4}[ -]?\d{4}',probe)
        if mobile:probe=probe[mobile.end():].strip(' ,，:：')
        if probe and fields.get('address') and normalize_cn_address(probe)['normalized_address']==fields['address']:
            start=line.find(probe)
            if start>=0:masked=masked[:start]+' '*len(probe)+masked[start+len(probe):]
        masked=re.sub(r'(?<!\d)1[3-9]\d[ -]?\d{4}[ -]?\d{4}(?!\d)',lambda m:' '*len(m.group()),masked)
        result.append(masked)
    return '\n'.join(result)


def merge_contact(draft, supplied):
    result = dict(draft or {})
    if supplied.get('_recipient_generated') is True and supplied.get('recipient'):
        result['_recipient_generated']=True
        result['_recipient_source']='ADMIN_TEMPORARY'
        result['_recipient_learning_excluded']=True
    elif supplied.get('recipient') and (supplied.get('recipient')!=result.get('recipient')
            or supplied.get('_recipient_source') in ('SOURCE_LOCAL','SOURCE_NICKNAME','SOURCE_AI')):
        for key in ('_recipient_generated','_recipient_source','_recipient_learning_excluded'):
            result.pop(key,None)
    for field in FIELDS:
        if field in supplied:
            result[field] = supplied[field]
    if supplied.get('raw_address'):
        result['raw_address'] = supplied['raw_address']
    if supplied.get('_name_absent_confirmed') and not result.get('recipient'):
        result['_name_absent_confirmed'] = True
    if supplied.get('_address_service_reason'):
        result['_address_service_reason']=str(supplied['_address_service_reason'])[:80]
    if '_address_cache_candidates' in supplied:
        result['_address_cache_candidates']=list(supplied['_address_cache_candidates'])[:4]
    if '_cloud_address_proposal' in supplied:
        result['_cloud_address_proposal']=supplied['_cloud_address_proposal']
    if supplied.get('_recipient_source') and not supplied.get('_recipient_generated'):
        result['_recipient_source']=supplied['_recipient_source']
    if supplied.get('_requires_real_name'):
        result['_requires_real_name']=True
    if supplied.get('_contact_source_conflicts'):
        result['_contact_source_conflicts']=list(supplied['_contact_source_conflicts'])
    if supplied.get('_suspected_recipient') and not result.get('recipient'):
        for key in ('_suspected_recipient','_suspected_recipient_source'):
            result[key]=supplied.get(key)
    # A customer-confirmed source candidate may still be attached to the
    # address collected before confirmation. Remove only this exact contact
    # suffix, proven by the original address/name/mobile adjacency. Never
    # trim a random generated name or a merely similar place name.
    candidate = str((draft or {}).get('_suspected_recipient') or '')
    source = str((draft or {}).get('_suspected_recipient_source') or '')
    phone = str(result.get('phone') or '')
    if (candidate and supplied.get('recipient') == candidate
            and valid_name(candidate) and name_has_evidence(candidate, source)
            and re.fullmatch(r'1[3-9]\d{9}', phone)):
        compact_source = re.sub(r'\s+', '', source)
        for key in ('address', 'raw_address'):
            value = str(result.get(key) or '')
            compact = re.sub(r'\s+', '', value)
            if (compact.endswith(candidate) and len(compact) > len(candidate)
                    and re.search(re.escape(compact) + r'[,，;；:：。.|]*'
                                  + re.escape(phone) + r'(?!\d)', compact_source)):
                result[key] = value[:-len(candidate)].rstrip()
    if result.get('recipient'):
        result.pop('_name_absent_confirmed', None)
        result.pop('_suspected_recipient',None)
        result.pop('_suspected_recipient_source',None)
    result['contact_diagnostics'] = diagnose_contact(result)
    return result


def needs_address_region(fields):
    """Concrete delivery detail exists, but its administrative chain is unsafe."""
    value = str(fields.get('raw_address') or fields.get('address') or '').strip()
    meta = normalize_cn_address(value)
    complete = all(meta.get(k) for k in ('province', 'city', 'detail')) and (meta.get('district') or meta.get('town'))
    return bool(value and not complete and re.search(r'小区|花园|公寓|学院|学校|苑|栋|幢|楼|室|\d+号', value))


def needs_address_detail(fields):
    value=str(fields.get('raw_address') or fields.get('address') or '').strip()
    meta=normalize_cn_address(value)
    return bool(value and meta.get('province') and meta.get('city')
                and (meta.get('district') or meta.get('town')) and not meta.get('detail'))


def supplement_address(fields, text):
    """Append proven missing administrative components; never replace detail."""
    original = str(fields.get('raw_address') or fields.get('address') or '').strip()
    addition = str(text or '').strip()
    old = normalize_cn_address(original)
    # A proven administrative chain with no delivery detail can accept an
    # explicit detail-only reply. Never replace its province/city/district.
    if original and old.get('province') and old.get('city') and (old.get('district') or old.get('town')) and not old.get('detail'):
        addition=re.sub(r'^(?:详细地址|补充地址|地址)\s*[:：]\s*','',addition)
        if (not addition or len(addition)>200
                or re.search(r'1[3-9]\d{9}|手机|电话|收件|商品|口味|取消|\d+\s*(?:支|根|件|个|盒|箱|套|包|份)',addition)
                or not re.search(r'路|街|小区|花园|公寓|学院|学校|苑|号|栋|幢|楼|室|单元',addition)):
            return None
        new=normalize_cn_address(addition)
        if any(new.get(k) for k in ('province','city','district')):
            return None
        combined=original+addition
        normalized=normalize_cn_address(combined)
        if not normalized.get('detail'):
            return None
        return dict(address=normalized['normalized_address'],raw_address=combined)
    # A supplement is region text only. A new delivery/detail, phone, or goods
    # belongs to another interaction and cannot overwrite this draft.
    if not needs_address_region(fields) or not re.search(r'省|市|区|县|街道|镇|乡', addition):
        return None
    if re.search(r'\d|小区|花园|公寓|栋|幢|楼|室|手机|电话|收件|商品|口味', addition):
        return None
    new = normalize_cn_address(addition)
    from region_index import get_region_index
    index = get_region_index()
    if old.get('district') and index and old['district'] not in index.region_by_name and old['district'] not in index.street_by_name:
        old['district'] = ''
    for key in ('province', 'city', 'district', 'town'):
        if old.get(key) and new.get(key) and old[key] != new[key]:
            return None
    if not any(new.get(k) and not old.get(k) for k in ('province','city','district','town')):
        return None
    detail = original
    for key in ('province','city','district','town'):
        value = str(old.get(key) or '')
        if value and detail.startswith(value):detail=detail[len(value):]
    prefix = ''.join(str(new.get(k) or old.get(k) or '') for k in ('province','city','district','town')
                     if (new.get(k) or old.get(k)) != '市辖区')
    combined = prefix + detail
    return dict(address=normalize_cn_address(combined)['normalized_address'], raw_address=combined)


def diagnose_contact(fields):
    missing, invalid = [], []
    address_issues=[]
    for field in FIELDS:
        value = str(fields.get(field) or '').strip()
        if not value:
            missing.append(field)
        elif field == 'recipient' and (not valid_name(value) or
                value.isdigit() and fields.get('_recipient_source') not in ('SOURCE_LOCAL','SOURCE_NICKNAME')):
            invalid.append(field)
        elif field == 'phone' and not re.fullmatch(r'1[3-9]\d{9}|0\d{9,11}', value):
            invalid.append(field)
        elif field == 'address':
            from cloud_address_trust import trusted
            if trusted(value) and not fields.get('_cloud_address_proposal'):
                continue
            from offline_address import assess
            decision=assess(value)
            address_issues=decision['issues']
            if not decision['valid']:invalid.append(field)
            if fields.get('_cloud_address_proposal') and field not in invalid:invalid.append(field)
    from recipient_policy import requires_real_name
    real_required=requires_real_name(fields)
    if real_required and fields.get('_recipient_generated') and 'recipient' not in invalid:
        invalid.append('recipient')
    result={'missing': missing, 'invalid': invalid, 'recognized': [f for f in FIELDS if f not in missing and f not in invalid]}
    if address_issues:result['address_issues']=address_issues
    if real_required and 'recipient' in missing+invalid:
        result['real_name_required']=True
    return result


def contact_prompt(diagnostics):
    names = {'recipient': '收件人姓名', 'phone': '收件人手机号', 'address': '完整收货地址'}
    missing, invalid = diagnostics['missing'], diagnostics['invalid']
    issues = ['缺少' + names[f] for f in missing]
    errors = {'recipient': '收件人姓名不符合规则，请填写有效姓名', 'phone': '收件人手机号格式不正确，请重新填写有效手机号', 'address': '收货地址不完整或无法识别，请填写省市区及详细地址'}
    if diagnostics.get('address_issues'):
        from offline_address import problem_text
        errors['address']=problem_text(diagnostics)
    issues.extend(errors[f] for f in invalid)
    if diagnostics.get('real_name_required'):
        issues.append('物流要求真实姓名，请补充实际收件人姓名；不能使用临时收件名称')
    prompt = '❌ 收件信息需要补充或修正：\n' + '\n'.join('• ' + x for x in issues)
    if len(missing) == 1 and not invalid:
        prompt += '\n就差' + names[missing[0]] + '，请补充。'
    return prompt
