"""Conservative message structure; never resolves products, prices or warehouses."""
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from parser_rules import normalize_order_text

_REF = re.compile(r'^(?:#\s*|订单\s*)?(\d{1,6})(?:[.、]|号订单)?$')
_WAREHOUSE = re.compile(r'^(?:(?:下单|发货|指定)?仓库\s*[:：]\s*)?((?:[A-Za-z]+\d+|[^\s:：，,]{1,20}仓))$')

def dispatch_reference_prefixes():
    """Reviewed external references, never warehouse routing instructions."""
    from rule_dictionary import runtime_store
    values=runtime_store().get('dispatch_reference_prefixes', [])
    if not isinstance(values,list):raise ValueError('发货备注前缀配置必须为列表')
    if any(not isinstance(v,str) or not re.fullmatch(r'[\u4e00-\u9fffA-Za-z]{1,12}',v) for v in values):
        raise ValueError('发货备注前缀配置无效')
    return tuple(sorted(set(values)))

def normalize_dispatch_markers(value, products=(), names=None):
    """Remove only complete dispatch-only notes; never infer routing or quantities."""
    protected=catalog_names(products) if names is None else names
    token=lambda value:re.sub(r'\s+','',unicodedata.normalize('NFKC',str(value))).casefold()
    identities={token(n) for n in protected if n}
    prefixes=dispatch_reference_prefixes()
    def note(candidate):
        if token(candidate) in identities:return None
        candidate=unicodedata.normalize('NFKC',candidate)
        stack=[];pairs={')':'(',']':'[','】':'【'}
        for char in candidate:
            if char in '([【':stack.append(char)
            elif char in pairs:
                if not stack or stack.pop()!=pairs[char]:return None
        if stack:return None
        candidate=re.sub(r'[()（）\[\]【】［］]',',',candidate)
        candidate=candidate.lstrip(' ,，;；')
        candidate=re.sub(r'^\s*(?:(?:下单|发货|指定)?仓库)\s*[:：]\s*','',candidate)
        candidate=re.sub(r'([A-Za-z]{1,8})\s+(?=\d)',r'\1',candidate)
        candidate=re.sub(r'(?<=\d)\s+(?=仓)', '',candidate)
        parts=[p for p in re.split(r'[,，、;；/\s]+',candidate.strip(' ,，;；。.')) if p]
        if not parts or any(token(p) in identities for p in parts):return None
        # x 3 / X3 is merchandise multiplication, not an external code.
        if any(re.fullmatch(r'[xX]\d+',p) for p in parts):return None
        pattern=r'(?:[A-Za-z]{1,8}\d{1,4}(?:仓)?|\d{1,3}仓|[一二两三四五六七八九十]+仓)'
        def registered(p):
            return any(re.fullmatch(re.escape(prefix)+r'(?:\d{1,4}(?:仓)?|仓)',p) for prefix in prefixes)
        if all(re.fullmatch(pattern,p) or registered(p) for p in parts):return '，'.join(parts)
        return None
    notes=[];lines=[]
    for line in str(value or '').splitlines():
        complete=note(line)
        if complete:
            notes.append(complete);continue
        # Never remove apparent codes inside a delivery address/contact line.
        if re.search(r'1[3-9]\d{9}|地址|收件|电话|手机|省|市|区|县|路|街|楼|栋|室',line):
            lines.append(line);continue
        # Peel nested dispatch-only parentheses without touching a mixed
        # goods/count/address expression. Each pass strictly shortens input.
        for _ in range(8):
            probe=syntax_text(line,protected)
            spans=[]
            for match in re.finditer(r'\([^()\n]*\)|（[^（）\n]*）|\[[^\[\]\n]*\]|【[^【】\n]*】|［[^［］\n]*］',probe):
                label=note(line[match.start():match.end()])
                if label:spans.append((match.start(),match.end(),label))
            if not spans:break
            for start,end,label in reversed(spans):
                line=line[:start]+' '+line[end:];notes.append(label)
        # Unwrapped inline notes need independent live merchandise evidence.
        # Only a complete prefix/suffix is removable, never a product count
        # or an interior unknown token. Catalog-free parsing stays conservative.
        def has_goods(fragment):
            view=syntax_text(fragment,protected)
            return '\ue000' in view and bool(re.search(r'\d',view))
        cuts=list(re.finditer(r'[,，、;；/\t ]+',line))
        for cut in reversed(cuts):
            label=note(line[:cut.start()])
            if label and has_goods(line[cut.end():]):
                notes.append(label);line=line[cut.end():];break
        for cut in re.finditer(r'[,，、;；/\t ]+',line):
            label=note(line[cut.end():])
            if label and has_goods(line[:cut.start()]):
                notes.append(label);line=line[:cut.start()];break
        lines.append(line)
    return '\n'.join(lines),tuple(notes)

def strip_category_enumeration(value, products=()):
    """Remove circled list bullets before NFKC can turn them into quantities."""
    identities={str(p.get(k) or '') for p in products for k in ('name','parent_name')}
    def replace(match):
        heading=match.group(2).strip()
        original=match.group(1)+heading
        if original in identities:return match.group()
        if heading in identities or re.search(r'板砖|鸭嘴|悦刻|高维|冰[爆暴]|莱爆|单颗蛋',heading):
            return heading+match.group(3)
        return match.group()
    return re.sub(r'(?m)^\s*([①-⑳])\s*([^\n:：]{1,40})([:：])',replace,str(value or ''))

def separate_fulfillment_metadata(value, names=()):
    """Whole-line notes only; never silently discard an unknown merchandise row."""
    lines=[]
    for line in str(value or '').splitlines():
        # Keycaps are list markers, not quantities. Do this before NFKC.
        if re.fullmatch(r'\s*[0-9#*]\ufe0f?\u20e3\s*',line):
            continue
        view=syntax_text(line,names).strip()
        whole=re.fullmatch(r'整单\s*(?:合计|总计|总数)\s*[:：]?\s*(\d+|[一二两三四五六七八九十]+)\s*(支|根|颗|个|件|条)?',view)
        if whole:
            lines.append('订单总数'+whole.group(1)+(whole.group(2) or '支'));continue
        # Protect exact catalog identities and any mixed goods/note expression.
        if ('缺货退' in view and fulfillment_parts(line,names)==('', '精准发货', None)):
            # Keep authorization evidence for the later structure/fulfillment
            # pass rather than deleting a formerly supported separator form.
            lines.append(line);continue
        note=view.strip('()（） ').strip()
        dispatch=r'(?:KFC\s*[一二两三四五六七八九十1-9]\s*[仓库])'
        instruction=r'(?:精准(?:发(?:货)?)?(?:谢谢)?|缺货退单|断货退回别随机)'
        if re.fullmatch(r'(?:'+dispatch+r'|'+instruction+r')(?:\s*[,，、;；]\s*(?:'+instruction+r'))*[。.!！]?',note,re.I):
            continue
        total=re.fullmatch(r'((?:共共|共计|总计|合计|小计|总共|一共|共|总|支数)\s*[:：]?\s*\d+\s*(?:支|根|颗|个|件|条)?)\s*(?:[（(]?\s*精准(?:发(?:货)?)?(?:谢谢)?\s*[）)]?)?',view)
        if total:
            # Retain the declared count so mismatch checks remain effective.
            count=re.sub(r'^(?:共共|支数)', '共',total.group(1))
            lines.append(count);continue
        lines.append(line)
    return '\n'.join(lines)

def is_order_receipt(value):
    """A forwarded success receipt is not a fresh purchase authorization."""
    text=str(value or '')
    return bool(re.search(r'(?m)^\s*(?:✅\s*)?(?:录单成功|下单成功)\s*(?:$|订单(?:号|编号)\s*[:：])',text)
                and re.search(r'订单(?:号|编号)\s*[:：]\s*\S+',text)
                and re.search(r'该笔订单|该笔金额|应付总额|今日订单金额|总计欠款|共\s*\d+\s*支',text))

_MONEY = re.compile(r'^(?:(?:商品金额|付款金额|合计金额|金额|总额|合计|付款|收款|货款|应付|实付|运费|优惠|余额|欠款)\s*[:：]?\s*[¥￥]?\s*\d+(?:\.\d{1,2})?\s*元?|[¥￥]\s*\d+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s*元)$')
_ARITHMETIC = re.compile(r'^[\d\s.+➕＋\-*/xX×÷=＝🟰()（）¥￥\ufe0f]+$')

_MONEY_LABELS = ('支付金额','实收金额','实收','总金额','订单金额','商品金额','付款金额','合计金额','应付总额','实付金额',
                 '金额合计','总价','合计货款','金额','总额','付款','收款','货款','应付','实付','运费','优惠','余额','欠款')


def _labelled_amount(line, names=()):
    """An explicitly labelled bill expression is metadata, not units.

    Full-line matching is intentional: an appended merchandise quantity must
    remain visible even when the customer starts the line with a money label.
    """
    if syntax_text(line, names) != line:
        return False
    labels = '|'.join(r'\s*'.join(map(re.escape, label)) for label in _MONEY_LABELS)
    number = r'[¥￥]?\s*\d+(?:\.\d{1,2})?'
    expression = number + r'(?:\s*[+\-*/×÷xX]\s*' + number + r')*(?:\s*=\s*' + number + r')?'
    pattern = r'(?:' + labels + r')\s*[:：=]?\s*' + expression + r'\s*(?:元|RMB|CNY)?'
    if re.fullmatch(pattern, line.strip(), re.I):
        return True
    match = re.match(pattern, line.strip(), re.I)
    if not match:
        return False
    tail = line.strip()[match.end():]
    return bool(re.fullmatch(r'(?:\s+|[（(\[【,，;；])[^\d\n]{1,100}', tail))

def _amount_with_note(line, names=()):
    """Only explicit money or an equality proves a numeric line is a bill.

    A catalog token anywhere on the line keeps the line in merchandise
    validation. Never infer money merely from a leading number or a '+'.
    """
    if syntax_text(line, names) != line:
        return False
    number = r'\d+(?:\.\d{1,2})?'
    arithmetic = (r'[¥￥]?\s*' + number +
                  r'(?:\s*[+\-*/×÷xX]\s*[¥￥]?\s*' + number + r')+\s*=\s*[¥￥]?\s*' + number)
    money = (r'(?:(?:商品金额|付款金额|合计金额|金额|总额|付款|收款|货款|应付|实付|运费|优惠|余额|欠款)'
             r'\s*[:：]?\s*[¥￥]?\s*' + number + r'|[¥￥]\s*' + number + r'|' + number + r'\s*元)')
    return bool(re.fullmatch(r'(?:' + arithmetic + '|' + money +
                             r')(?:\s*元)?(?:\s+.*|\s*[（(\[【,，;；¥￥].*)', line.strip()))
_CONTACT = re.compile(r'1[3-9]\d{9}|收件人|手机号|手机号码|详细地址|地址')
_FULFILLMENT = re.compile(r'(?:缺货(?:随机|替换|备选|备|换)|没货(?:随机|换)|备选(?:口味(?=\s*[:：]))?|备用|替补|(?<![\u4e00-\u9fff])备)(.*)$')
_CONDITIONAL = re.compile(r'(?:如果)?(?:缺货|没货|没有|缺的话)(?:的话)?(?:就)?(?:换|发)?(.+)$')
_REVERSE_CONDITIONAL = re.compile(r'(?:没有|没(?!有))([^，,：:\s]+?)就(?:换|发)?(.+)$')

def _catalog_names_memo_key(products=(),rows=None):
    if rows is None and products:
        from rule_dictionary import runtime_store
        rows=runtime_store().get('aliases',[])
    product_fields=('id','name','flavor','parent_name','category','parent_id','default_warehouse_id','is_archived','is_active')
    rule_fields=('type','status','alias','standard','category','scope','warehouse_id','category_id','product_id')
    return (tuple(tuple(p.get(k) for k in product_fields) for p in products or ()),
            tuple(tuple(r.get(k) for k in rule_fields) for r in rows or ()))

from request_memo import request_memoized

@request_memoized(_catalog_names_memo_key)
def catalog_names(products=(), rows=None):
    """Only real catalog identities and effective aliases can shield syntax."""
    live=[dict(p,name=p.get('name') or p.get('flavor'),parent_name=p.get('parent_name') or p.get('category'))
          for p in products or () if not p.get('is_archived') and p.get('is_active',True)]
    names={str(p.get(key) or '') for p in live for key in ('name','parent_name')}
    by_category = {}
    by_flavor = {}
    for product in live:
        by_category.setdefault(product.get('parent_name'), []).append(product)
        by_flavor.setdefault((product.get('parent_name'), product.get('name')), []).append(product)
    if live:
        from brand_aliases import scope_matches
        if rows is None:
            from rule_dictionary import runtime_store
            rows=runtime_store().get('aliases',[])
        for row in rows:
            if row.get('status')!='active':continue
            kind=row.get('type')
            candidates = (by_category.get(row.get('standard'), []) if kind == 'MODEL_ALIASES'
                else by_flavor.get((row.get('category'), row.get('standard')), []) if kind == 'EXPLICIT_FLAVOR' else [])
            valid=any(scope_matches(row,p) for p in candidates)
            if valid:names.add(str(row.get('alias') or ''))
    return tuple(sorted({normalize_order_text(n) for n in names if n},key=lambda n:(-len(n),n)))

@lru_cache(maxsize=64)
def _syntax_pattern(names):
    names=tuple(sorted(set(names),key=lambda n:(-len(n),n)))
    pattern='|'.join(re.escape(n) for n in names if n)
    return re.compile(pattern, re.IGNORECASE) if pattern else None


def syntax_text(line, names=()):
    """Longest complete tokens are opaque to control syntax; offsets survive."""
    if not names:return line
    pattern=_syntax_pattern(tuple(names))
    if pattern is None:return line
    return pattern.sub(lambda m:'\ue000'*len(m.group()),line)

def fulfillment_parts(line, names=()):
    """Keep original authorization evidence; do not resolve any catalog names."""
    syntax=syntax_text(line,names)
    # A complete pair of dispatch instructions joined with +/punctuation is
    # not an unquantified flavor. Shield live identities first and require
    # every segment to be known, positive syntax. Unknown/negated/mixed goods
    # remain untouched. Exact dispatch dominates shortage-return wording;
    # this grants no substitution, deletion or warehouse selection authority.
    whole=syntax.strip()
    wrapped=re.fullmatch(r'\(([^()]*)\)|（([^（）]*)）',whole)
    if wrapped:whole=next(g for g in wrapped.groups() if g is not None).strip()
    pieces=[p for p in re.split(r'\s*[,，、;；+＋]\s*|\s+',whole) if p]
    precise=lambda p:bool(re.fullmatch(r'精准(?:发(?:货)?)?(?:谢谢)?',p))
    shortage=lambda p:bool(re.fullmatch(r'缺货退(?:单)?',p))
    if (len(pieces)>=2 and all(precise(p) or shortage(p) for p in pieces)
            and any(precise(p) for p in pieces) and any(shortage(p) for p in pieces)):
        return '', '精准发货', None
    mutual=re.search(r'\s*[（(]?\s*(?:互为备选|互相备选(?:口味)?)\s*[）)]?\s*$',syntax)
    if mutual and not re.search(r'(?:不|非|禁止|不可|不要)\s*(?:互为备选|互相备选(?:口味)?)',syntax):
        return line[:mutual.start()].strip(), '互为备选', None
    precise = re.search(r'(?:^|[\s，,])精准(?:发货|发)\s*$', syntax)
    if precise:
        return line[:precise.start()].strip().rstrip('，,'), '精准发货', None
    # A shortage refusal is not permission to substitute. Require a positive
    # replacement target rather than interpreting the negated '发' as one.
    if re.search(r'(?:缺货|没货|没有)(?:的话)?(?:就)?(?:不发|别发|不要发|不换|不要换|取消|退单)',syntax):
        return None
    reverse=_REVERSE_CONDITIONAL.search(syntax)
    if reverse:
        return (line[:reverse.start()].strip(),line[slice(*reverse.span(2))],line[slice(*reverse.span(1))])
    match=_FULFILLMENT.search(syntax) or _CONDITIONAL.search(syntax)
    if not match:return None
    prefix=line[:match.start()].strip().rstrip('，,').strip()
    # Repeated preferred name inside '如果X缺货' is not a second item.
    condition=syntax[:match.start()].find('如果')
    if condition>=0:prefix=line[:condition].rstrip('，,').strip()
    start,end=match.span(1)
    leading=re.match(r'^(?:的话)?(?:就)?(?:换|发)?',syntax[start:end])
    start+=leading.end()
    trailing=re.search(r'(?:都行|均可|也行|即可)\s*$',syntax[start:end])
    if trailing:end=start+trailing.start()
    body=line[start:end].strip(' :：()（）')
    return prefix,body,None
_CN = {'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}

def _number(match):
    value=match.group(1)
    strict=r'(?:[零一二两三四五六七八九]|十|十[一二两三四五六七八九]|[二两三四五六七八九]十(?:[一二两三四五六七八九])?)'
    if not re.fullmatch(strict,value):return match.group(0)
    if value in _CN:n=_CN[value]
    elif value=='十':n=10
    elif value.startswith('十'):n=10+_CN[value[1]]
    else:n=_CN[value[0]]*10+(_CN[value[2]] if len(value)==3 else 0)
    return str(n)+match.group(2)

@dataclass(frozen=True)
class OrderStructure:
    text: str
    customer_order_ref: str = None
    customer_warehouse_notes: tuple = ()
    customer_amount_notes: tuple = ()
    fulfillment_notes: tuple = ()

def structure_order_text(value, products=(), names=None):
    # Canonicalize only a complete 3-4-4 mainland mobile before any quantity
    # parser sees it. The original message remains the evidence source.
    normalized=re.sub(r'(?<!\d)(1[3-9]\d[ -]?\d{4}[ -]?\d{4})(?!\d)',
                      lambda m:re.sub(r'[ -]','',m.group(1)),strip_category_enumeration(value,products))
    normalized=re.sub(r'(?m)^\s*下单产品\s*[:：]\s*', '',normalized)
    # A dispatch prefix before an explicit administrative address is a note,
    # not an address component. Preserve it on its own line so every parser
    # sees the same boundary; never infer a routing warehouse from this code.
    normalized=re.sub(
        r'(?m)^\s*((?:[A-Za-z]+\d+|[一二三四五六七八九十两]+)仓)\s+(?=[\u4e00-\u9fff]{2,12}(?:省|市|自治区))',
        lambda m:m.group(1)+'\n',normalized)
    # A whole-line warehouse plus order total has two roles, neither a SKU.
    # Accept glued notes without borrowing the preceding category scope.
    protected_names=catalog_names(products) if names is None else names
    normalized=separate_fulfillment_metadata(normalized,protected_names)
    normalized,dispatch_notes=normalize_dispatch_markers(normalized,products,protected_names)
    explicit_notes=[];goods_lines=[]
    for line in normalized.splitlines():
        if re.match(r'^\s*(?:客户备注|发货备注|仓库备注|记账说明|备注|仓库)\s*[:：]',syntax_text(line,protected_names)):
            explicit_notes.append(line)
        else:goods_lines.append(line)
    normalized='\n'.join(goods_lines)
    from numeric_metadata import separate
    normalized,numeric_refs,numeric_amounts=separate(normalized,products,protected_names)
    # Explicit multiplication before a number; do not rewrite a symbol inside
    # a real catalog identity or share this adapter rule with wholesale.
    chars=list(normalized)
    for match in re.finditer(r'[✖✕]\ufe0f?(?=\s*\d+)',syntax_text(normalized,protected_names)):
        chars[match.start()]='×'
        for index in range(match.start()+1,match.end()):chars[index]=' '
    normalized=''.join(chars)
    interleaved_notes=list(dispatch_notes)+explicit_notes
    # Explicit Chinese warehouse suffix after a quantified item is metadata,
    # including the first catalog-free parse. Never strip a real catalog name.
    suffix_lines=[]
    for source_line in normalized.splitlines():
        match=re.fullmatch(r'(.+?\d+\s*(?:支|根|个|件|只|条)?)[\t ，,;；]+([一二两三四五六七八九十]+仓)\s*',source_line)
        if (match and not re.search(r'1[3-9]\d{9}|地址|收件|电话|手机|省|市|区|县|路|街|楼|栋|室',source_line)
                and syntax_text(match.group(2),protected_names)==match.group(2)):
            suffix_lines.extend((match.group(1),match.group(2)))
        else:suffix_lines.append(source_line)
    normalized='\n'.join(suffix_lines)
    merchandise_lines=[]
    for source_line in normalized.splitlines():
        probe=syntax_text(source_line,protected_names)
        # Only an established merchandise line can contain an embedded note.
        # Shield complete catalog identities (even those containing 仓/KFC),
        # and leave addresses/contact spans entirely untouched.
        if ('\ue000' not in probe or re.search(r'1[3-9]\d{9}|地址|收件|电话|手机|省|市|区|县|路|街|楼|栋|室',probe)):
            merchandise_lines.append(source_line);continue
        spans=[]
        for match in re.finditer(r'[A-Za-z]+\d+仓|(?<![A-Za-z])KFC(?![A-Za-z])',probe,re.I):
            spans.append(match.span())
        masked=list(probe)
        for start,end in spans:masked[start:end]='\ue000'*(end-start)
        probe=''.join(masked)
        # A unit belongs to the adjacent count, not the free-form note.
        probe=re.sub(r'\d+\s*(?:个|支|根|件|只|条)?',lambda m:'\ue000'*len(m.group()),probe)
        spans.extend(m.span() for m in re.finditer(r'[A-Za-z\u4e00-\u9fff]{1,20}仓',probe))
        line_notes=[]
        for start,end in sorted(spans,reverse=True):
            line_notes.append(source_line[start:end])
            source_line=source_line[:start]+' '+source_line[end:]
        interleaved_notes.extend(reversed(line_notes))
        # Parentheses containing only a removed customer note carry no count.
        source_line=re.sub(r'[（(]\s*[）)]',' ',source_line)
        if spans:source_line=re.sub(r'[（(]\s*[,，;；]*\s*$','',source_line)
        if spans:source_line=source_line.lstrip(' \t:：,，;；-—–')
        merchandise_lines.append(source_line)
    normalized='\n'.join(merchandise_lines)
    warehouse_totals=[]
    for line in normalized.splitlines():
        combined=re.fullmatch(r'\s*([^\s:：，,;；()（）]{1,20}仓)[\s:：，,;；]*'
            r'((?:共计|总共|总计|合计|一共|共)\s*[:：]?\s*\d+\s*(?:支|只|根|个|件|条)?)[ \t，,;；。]*',line)
        if (combined and normalize_order_text(line).casefold() not in {n.casefold() for n in protected_names}
                and not re.search(r'地址|省|市|区|县|路|街|楼|栋|室',combined.group(1))):
            total=combined.group(2)
            if not re.search(r'(?:支|只|根|个|件|条)\s*$',total):total+='支'
            warehouse_totals.extend((combined.group(1),total))
        else:warehouse_totals.append(line)
    normalized='\n'.join(warehouse_totals)
    # Split only a proven warehouse prefix followed by catalog syntax and an
    # explicit quantity. The prefix remains a note, never routing authority.
    if products:
        known = catalog_names(products)
        category_names = {normalize_order_text(str(p.get('parent_name') or p.get('category') or ''))
                          for p in products if p.get('parent_name') or p.get('category')}
        product_syntax = tuple(sorted(set(known) | category_names, key=lambda n:(-len(n),n)))
        from category_evidence import _heading_match
        def starts_identity(value):
            value=normalize_order_text(value)
            return any(_heading_match(n,value) or value.casefold().startswith(n.casefold()) for n in product_syntax)
        rewritten=[]
        for source_line in normalized.splitlines():
            match=re.match(r'^\s*([^\s:：，,;；()（）]{1,20}仓)[\s:：，,;；]*(.+)$',source_line)
            remainder=match.group(2) if match else ''
            if (match and not any(name.casefold().startswith(match.group(1).casefold()) for name in product_syntax)
                    and starts_identity(remainder)
                    and not re.search(r'1[3-9]\d{9}|地址|省|市|区|县|路|街|楼|栋|室',source_line)):
                rewritten.extend((match.group(1),remainder))
            else:
                trailing=re.fullmatch(r'(.+?)[\s，,;；]+([^\s:：，,;；()（）]{1,20}仓)',source_line)
                if (trailing and starts_identity(trailing.group(1))
                        and not re.search(r'1[3-9]\d{9}|地址|省|市|区|县|路|街|楼|栋|室',source_line)
                        and not any(n.casefold()==source_line.strip().casefold() for n in product_syntax)):
                    rewritten.extend(trailing.groups())
                else:rewritten.append(source_line)
        normalized='\n'.join(rewritten)
    # A parenthesized count is a declaration, not a trailing customer note.
    # Separate it before catalog preparation/tokenization so it cannot hide
    # the preceding item's quantity or disappear with prose parentheses.
    normalized=re.sub(r'[（(]\s*((?:共计|共|合计|总计|总共|一共)\s*[:：]?\s*\d+\s*(?:根|支|件|个|条))'
        r'\s*[,，;；]\s*(精准发货)\s*[）)]',lambda m:'\n'+m.group(1)+'\n'+m.group(2)+'\n',normalized)
    normalized=re.sub(
        r'[（(]\s*((?:共计|共|合计|总计|总共|一共)\s*[:：]?\s*'
        r'(?:\d+|[零一二两三四五六七八九十]{1,3})\s*(?:根|支|件|个|条)?)\s*(?:[）)]|(?=\n|$))',
        lambda m:'\n'+m.group(1).strip()+('' if re.search(r'(?:根|支|件|个|条)\s*$',m.group(1)) else '支')+'\n',normalized)
    # Separate a delimited terminal declaration before any compact merchandise
    # tokenizer can consume its control word as a flavor. Identity shielding
    # preserves genuine catalog names containing these words.
    protected=catalog_names(products) if names is None else names
    # Parenthesized external dispatch codes have their own role, even when
    # embedded between a model and a flavor. Preserve, never route by them.
    for match in reversed(list(re.finditer(r'[（(]\s*[A-Za-z]+\d{1,3}仓\s*[）)]',syntax_text(normalized,protected)))):
        note=normalized[match.start()+1:match.end()-1].strip()
        normalized=normalized[:match.start()]+'\n'+note+'\n'+normalized[match.end():]
    declaration_lines=[]
    for source_line in normalized.splitlines():
        probe=syntax_text(source_line,protected)
        terminal=re.search(r'(?<=[\s,，、;；])((?:共计|总共|一共|合计|总计|共|合)\s*[:：]?\s*\d+\s*(?:根|支|件|个|只|条)?)\s*$',probe)
        prefix=probe[:terminal.start()] if terminal else ''
        if (terminal and not re.search(r'地址|电话|手机|收件|收货|省|市|区|县|楼|栋|室',prefix)
                and (re.search(r'[^\d\s]\s*(?:[×xX*]\s*)?\d+\s*(?:根|支|件|个|只|条)?(?:\s|[,，、;；]|$)',prefix)
                     or _FULFILLMENT.search(prefix))):
            declaration=source_line[terminal.start():].strip()
            declaration=re.sub(r'^合(?=\s*[:：]?\s*\d)','合计',declaration)
            declaration_lines.extend((source_line[:terminal.start()].rstrip(' ,，、;；'),declaration))
        else:declaration_lines.append(source_line)
    normalized='\n'.join(declaration_lines)
    if products:
        from category_evidence import prepare
        from rule_dictionary import runtime_store
        from typo_confirmation import category_candidates
        scoped=[]
        for source_line in normalized.splitlines():
            heading=re.fullmatch(r'\s*([^\s:：]{2,10})\s+(.+)',source_line)
            if heading and len(category_candidates(heading.group(1),products))==1 and re.search(r'\d',heading.group(2)):
                scoped.extend(('品类：'+heading.group(1),heading.group(2)))
            else:scoped.append(source_line)
        normalized='\n'.join(scoped)
        normalized=prepare(normalized,products,runtime_store().get('aliases',[]))
    lines=normalize_order_text(normalized).splitlines()
    names=catalog_names(products) if names is None else names
    nonempty=[i for i,line in enumerate(lines) if line.strip()]
    ref=numeric_refs[0] if numeric_refs else None
    # A leading independent reference needs an actual contact/order body. Never
    # strip a quantity attached to a product or a standalone reply to a draft.
    if len(nonempty)>1:
        first=nonempty[0];m=_REF.fullmatch(lines[first])
        if m and _CONTACT.search('\n'.join(lines[first+1:])):
            ref=m.group(1);lines[first]=''
    kept=[];warehouses=list(interleaved_notes);amounts=list(numeric_amounts);fulfillment_notes=[]
    # Bare alphabetic labels are auxiliary only after a quantified goods
    # section in a complete contact-bearing message. Never strip a name or a
    # current catalog token merely because it uses Latin letters.
    has_contact=bool(_CONTACT.search('\n'.join(lines)))
    goods_seen=False
    trailing_amount = None
    if len(nonempty)>2 and _CONTACT.search('\n'.join(lines)):
        last=nonempty[-1]
        # An auxiliary trailing number after a catalog-proven or quantified
        # product section is not quantity across intervening contact fields.
        # Missing product counts remain missing. A standalone
        # quantity reply and a labelled/numbered address are never removed.
        if (re.fullmatch(r'\d+(?:\.\d{1,2})?',lines[last].strip())
                and (re.search(r'[^\d\s]\s*(?:×|x)?\s*\d+\s*(?:根|支|件|个)?(?:\s|$)',
                              '\n'.join(lines[i] for i in nonempty[:-1] if not _CONTACT.search(lines[i])
                                        and not re.search(r'地址|省|市|区|县|街道|楼|栋|室',lines[i])))
                     or any(name and name.casefold() in lines[i].casefold()
                            for i in nonempty[:-1] if not _CONTACT.search(lines[i])
                            and not re.search(r'地址|省|市|区|县|街道|楼|栋|室',lines[i])
                            for name in protected))):
            trailing_amount=last
    for index,line in enumerate(lines):
        if not line.strip():continue
        if goods_seen and re.fullmatch(r'\s*合\s*[:：]?\s*\d+\s*(?:根|支|件|个|只|条)?\s*',syntax_text(line,names)):
            line=re.sub(r'^\s*合','合计',line)
        # Explicit postal metadata is contact data, never a merchandise row.
        # Shield real catalog labels before interpreting metadata syntax.
        if re.fullmatch(r'\s*(?:邮政编码|邮编|postal\s*code|zip\s*code)\s*[:：]?\s*\d{4,10}\s*',syntax_text(line,names),re.I):
            continue
        # A trailing price plus dispatch code follows quantified goods. Keep
        # both as customer notes, not compact items (e.g. 210 -> 2 x 10).
        price_dispatch=re.fullmatch(r'\s*([¥￥]?\d+(?:\.\d{1,2})?)\s+([A-Za-z]+\d{1,3}(?:仓)?)\s*',line)
        warehouse_amount=re.fullmatch(r'\s*([一二两三四五六七八九十]+仓|[A-Za-z]+\d{1,3}仓)\s*[¥￥]?(\d+(?:\.\d{1,2})?)\s*元?\s*',line)
        if warehouse_amount and syntax_text(line,names)==line:
            warehouses.append(warehouse_amount.group(1));amounts.append(warehouse_amount.group(2));continue
        if price_dispatch and has_contact and goods_seen and re.fullmatch(
                r'\s*[¥￥]?\d+(?:\.\d{1,2})?\s+[A-Za-z]+\d{1,3}(?:仓)?\s*',syntax_text(line,names)):
            amounts.append(price_dispatch.group(1));warehouses.append(price_dispatch.group(2));continue
        numeric_warehouse=re.fullmatch(r'\s*(\d{1,3}\s*仓库|仓库\s*\d{1,3})\s*',line)
        if numeric_warehouse and normalize_order_text(line).casefold() not in {n.casefold() for n in names}:
            warehouses.append(numeric_warehouse.group(1));continue
        # An entire dispatch annotation has no merchandise quantity. Limit
        # this to the warehouse-code syntax already accepted by _WAREHOUSE;
        # preserve the code as a note, never invent a route or Alias.
        dispatch=re.fullmatch(r'\s*(?:[（(]\s*)?(?:下|下单到|发往|发货到)?\s*'
                              r'([A-Za-z]+\s*\d+)\s*(?:仓)?\s*(?:正品)?\s*'
                              r'(?:[）)]\s*)?((?:共|共计|合计|总计)\s*\d+\s*(?:支|只|根|个|件))?\s*',line)
        decorated_dispatch=(re.match(r'^\s*(?:下|发|[（(])',line)
                            or (dispatch and dispatch.group(2))
                            or re.fullmatch(r'\s*[A-Za-z]+\s+\d{1,3}\s*',line))
        if (dispatch and decorated_dispatch and not _CONTACT.search(line)
                and len(re.sub(r'\D','',dispatch.group(1)))<=3
                and normalize_order_text(line).casefold() not in {n.casefold() for n in names}):
            warehouses.append(re.sub(r'\s+','',dispatch.group(1)))
            if not dispatch.group(2):continue
            line=dispatch.group(2)
        # Normalize the customer count unit only in a complete declaration;
        # never rewrite "只" inside a live flavor/name or address.
        line=re.sub(r'^((?:(?:共计|共|合计|总计|总共|一共)\s*)?\d+)\s*只\s*$',r'\1支',line.strip())
        line=re.sub(r'^总数量\s*[:：]?\s*(\d+)(?:支|根|件|个)?$',r'总计\1支',line.strip())
        # A declared count and a trailing delivery/business note have separate
        # roles. Keep the count intact instead of parsing its prefix as flavor.
        declared=re.fullmatch(r'((?:共计|总共|总计|合计|一共|共)\s*[:：]?\s*(?:\d+|[零一二两三四五六七八九十]+)\s*(?:支|根|个|件|条))\s*[,，;；]\s*(.+)',line)
        if declared and _WAREHOUSE.fullmatch(re.sub(r'^(?:发|发货到|发往|送到)\s*','',declared.group(2))):
            line=declared.group(1)
            amounts.append(declared.group(2))
        if index==trailing_amount:
            amounts.append(line);continue
        warehouse=_WAREHOUSE.fullmatch(line)
        catalog_token=normalize_order_text(line).casefold() in {n.casefold() for n in names}
        unwrapped_note=re.sub(r'^正品\s*','',line.strip().strip('()（）[]【】,，;；。. '))
        dispatch_parts=[p.strip() for p in re.split(r'[,，、;；\s]+',unwrapped_note) if p.strip()]
        if (not catalog_token and dispatch_parts and all(re.fullmatch(
                r'(?:[A-Za-z]{1,8}\d{1,4}(?:仓)?|\d{1,3}仓|[一二两三四五六七八九十]+仓|[^\d\s:：，,]{1,12}仓)',p)
                for p in dispatch_parts) and not any(p.casefold() in {n.casefold() for n in names} for p in dispatch_parts)):
            warehouses.append(unwrapped_note);continue
        if (not catalog_token and re.fullmatch(r'(?:[^\s:：，,;；()（）]{1,20}仓\s*)+',unwrapped_note)
                and not _CONTACT.search(line) and not re.search(r'省|市|区|县|路|街|楼|栋|室',unwrapped_note)):
            warehouses.append(unwrapped_note);continue
        # Whole-line business/shipping annotations have no merchandise count.
        # Catalog names remain protected; quantified legitimate Kfc items stay.
        note_view=syntax_text(line,names).strip()
        if re.match(r'^(?:客户备注|发货备注|仓库备注|记账说明|备注|仓库)\s*[:：]',note_view):
            warehouses.append(line);continue
        if (not catalog_token and '\ue000' not in note_view
                and not re.search(r'(?:[×xX*]\s*\d|\d+\s*(?:支|只|件|个|盒|根))',note_view)
                and (re.fullmatch(r'.{0,30}?(?:记账|挂账|账单备注|发货备注|仓库备注)[。.!！]?',note_view,re.I)
                     or re.fullmatch(r'(?:客户备注|发货备注|仓库备注|备注)\s*[:：].+',note_view))):
            warehouses.append(line);continue
        nearby_contact = any(_CONTACT.search(lines[j]) for j in (index-1,index+1)
                             if 0 <= j < len(lines))
        auxiliary_label=(has_contact and goods_seen and not nearby_contact and
                         re.fullmatch(r'[A-Za-z]{2,20}',line.strip()))
        if not catalog_token and (warehouse or auxiliary_label):
            warehouses.append(warehouse.group(1) if warehouse else line.strip());continue
        shipping_note=re.fullmatch(r'\s*(?:(?:订单|编号|序号)?\s*\d{1,6}\s*[.、:：#-]?\s*)?(?:京东|顺丰|中通|圆通|申通|韵达|极兔|邮政|EMS|快递)?\s*(?:寄付|到付|现付|运费寄付|运费到付)\s*[。.]?\s*',line,re.I)
        if ((shipping_note and not catalog_token) or _labelled_amount(line,names) or _MONEY.fullmatch(line) or
                (_ARITHMETIC.fullmatch(line) and re.search(r'[=＝🟰+➕＋*/×÷]',line) and syntax_text(line,names)==line) or
                _amount_with_note(line,names)):
            amounts.append(line);continue
        if (not _CONTACT.search(line) and not re.search(r'地址|省|市|区|县|街道|楼|栋|室',line)
                and re.search(r'[^\d\s]\s*(?:×|x)?\s*\d+\s*(?:根|支|件|个)?(?:\s|[,，、]|$)',line)):
            goods_seen=True
        directive=fulfillment_parts(line,names)
        backup_dispatch=re.search(r'[\s,，;；]+([^\s:：，,;；()（）]{1,20}仓)\s*$',line)
        if (directive and backup_dispatch and not _CONTACT.search(line)
                and normalize_order_text(backup_dispatch.group(1)).casefold() not in {n.casefold() for n in names}):
            warehouses.append(backup_dispatch.group(1))
            line=line[:backup_dispatch.start()].rstrip(' ,，;；')
            directive=fulfillment_parts(line,names)
        if directive and not re.search(r'地址|收件|手机|电话',line):
            if directive[1].strip() not in ('无','没有','无备选','暂无'):
                fulfillment_notes.append(line)
            line=directive[0].strip().rstrip('（(').strip()
            if directive[2] and directive[2] not in line:line=(line+' '+directive[2]).strip()
            if not line:continue
        # An explicit preference label is syntax. Shield catalog identities
        # first so a legitimate flavor beginning with 首选 is never shortened.
        preferred=re.match(r'^\s*首选\s*[:：]?\s*',syntax_text(line,names))
        if preferred:line=line[preferred.end():]
        for match in reversed(list(re.finditer(r'([零一二两三四五六七八九十]{1,3})(根|支|件|个|条)',syntax_text(line,names)))):
            count=_number(match)
            if match.start() and line[match.start()-1].isdigit():count='×'+count
            line=line[:match.start()]+count+line[match.end():]
        # A trailing prose note cannot hide an explicit merchandise count.
        # Authorization has already been extracted above; original raw text
        # remains intact for the source-evidence validator.
        line=re.sub(r'(\d+\s*(?:根|支|件|个)?)[（(][^()（）\d]*[）)]\s*$',r'\1',line)
        # A colon followed by named item quantities forms an explicit section.
        # Contact labels and numeric-only values are not product sections.
        section=re.fullmatch(r'([^:：]+)[:：]\s*(.+)',line)
        if (section and section.group(1).strip() not in ('品类','型号')
                and not re.search(r'收件|姓名|手机|电话|地址|地区',section.group(1))
                and re.search(r'[^\d\s]\s*\d+\s*(?:根|支|件|个)?(?:[,，、]|$)',section.group(2))):
            header=section.group(1).strip()
            subtotal=re.search(r'(\d+)\s*(根|支|件|个)\s*$',header)
            if subtotal:header=header[:subtotal.start()].strip()
            kept.append(header)
            kept.extend(p.strip() for p in re.split(r'[,，、]',section.group(2)) if p.strip())
            if subtotal:kept.append('小计'+subtotal.group(1)+subtotal.group(2))
            continue
        kept.append(line)
    return OrderStructure('\n'.join(kept),ref,tuple(warehouses),tuple(amounts),tuple(fulfillment_notes))

def quantity_text(value, products=()):
    # A naked number has no explicit total semantics. The shared parser's
    # historical policy remains unchanged; only this adapter removes it.
    lines=[]
    from order_parser import OrderParser
    from contact_fields import merchandise_text
    for line in structure_order_text(merchandise_text(value,contact_labels=True,products=products),products).text.splitlines():
        from parser_rules import normalize_item_candidate
        line = normalize_item_candidate(line)
        # A standalone numeric label has no total semantics. Check before
        # the legacy declaration adapter, which accepts bare digits as totals.
        if re.fullmatch(r'\d+',line.strip()):continue
        from parser_rules import quantity_declaration
        if quantity_declaration(line):
            lines.append(line)
            continue
        if '只' in line:
            # Adapter-only unit spelling; shared wholesale rule source and
            # its synchronization hash remain unchanged. Protect real labels.
            chars=list(line)
            for match in re.finditer(r'\d+\s*只(?=$|[\s,，、;；])',syntax_text(line,catalog_names(products))):
                chars[match.end()-1]='支'
            line=''.join(chars)
        if not _CONTACT.search(line):
            line = re.sub(r'([*×xX])\s+(?=\d)',r'\1',line)
        if re.fullmatch(r'\d+',line.strip()):continue
        # Scope validation must see every flavor quantity, not just the last
        # number on a whitespace/comma-separated merchandise line.
        # Delimiters separate complete items; whitespace can be inside one
        # item's "flavor 2个" and must not split its name from its count.
        delimited=[normalize_item_candidate(p) for p in re.split(r'[,，、;；]+',line) if p.strip()]
        parts=(delimited if len(delimited)>1 and all(OrderParser._parse_item_quantity(p) for p in delimited)
               else [normalize_item_candidate(p) for p in re.split(r'[\s,，、;；]+',line) if p.strip()])
        # Expand EACH delimited fragment, not only a whole single-fragment
        # line. The final fragment may itself contain adjoining items.
        # The old suffix parser accepted "甲1乙2" as one item of quantity 2.
        expanded=[]
        for part in parts:
            masked=syntax_text(part,catalog_names(products))
            opaque=re.fullmatch(r'\ue000+\s*[*×xX]?\s*\d+\s*(?:根|支|只|个|件|颗|盒)?',masked)
            compact=OrderParser._parse_compact_item_line(part,None) if not opaque else []
            expanded.extend(str(n)+'×'+str(q) for n,q in compact) if compact else expanded.append(part)
        parts=expanded
        if (len(parts)>1 and not _CONTACT.search(line)
                and not re.search(r'地址|省|市|区|县|路|街|楼|栋|室',line)
                and all(OrderParser._parse_item_quantity(p) for p in parts)):
            lines.extend(parts)
        else:
            # Reuse the merchandise tokenizer for adjoining items such as
            # "口味甲3口味乙2". Treating this as one suffix-count line loses
            # the first quantity and makes the subtotal incorrectly equal 2.
            compact=[]
            if (not _CONTACT.search(line) and not re.search(r'地址|省|市|区|县|路|街|楼|栋|室',line)
                    and not re.match(r'^(?:品类|型号)[:：]',line)):
                from order_parser import OrderParser
                compact=OrderParser._parse_compact_item_line(line,None)
            if compact:
                lines.extend(str(name)+'×'+str(quantity) for name,quantity in compact)
            else:lines.append(line)
    # Shared scope rules understand suffix counts. Preserve explicit prefix
    # unit counts of both known and unresolved flavors in that same grammar.
    protected=catalog_names(products)
    for index,line in enumerate(lines):
        match=re.fullmatch(r'(\d+)\s*(?:支|只|根|个|件)\s*([^\d,，、;；:：]+)',line)
        if (match and line not in protected and not re.search(
                r'地址|省|市|区|县|街道|路|楼|栋|幢|单元|室|电话|手机',syntax_text(line,protected))):
            tail=match[2].strip()
            # A category heading is a section declaration, not a flavor.
            if tail not in {str(p.get('parent_name') or '') for p in products}:
                lines[index]=tail+'×'+match[1]
    # User-approved contract (2026-10-24): an ORIGINAL standalone terminal
    # 共N支/根 after multiple merchandise categories is an order total.
    # Decide from syntax/scope, never from whether N matches the computed sum.
    # Inline category declarations and explicit 小计 remain section-local.
    original_lines=[line.strip() for line in normalize_order_text(value).splitlines() if line.strip()]
    footer=re.fullmatch(r'共\s*([0-9零一二两三四五六七八九十]+)\s*(支|根)[ \t，,;；。]*',
                        original_lines[-1]) if original_lines else None
    if footer and lines:
        categories={str(p.get('parent_name') or p.get('category') or '') for p in products
                    if p.get('is_active',True) and not p.get('is_archived')}
        active=None;item_sections=set()
        for line in lines[:-1]:
            heading=re.fullmatch(r'(?:品类|型号)[:：]\s*(.+)',line)
            if heading:
                active=heading[1] if heading[1] in categories else None
            elif active and not quantity_declaration(line) and OrderParser._parse_item_quantity(line):
                item_sections.add(active)
        number=footer[1]
        if not number.isdigit():
            from learning_knowledge import _chinese_quantity
            number=_chinese_quantity(number.replace('两','二'))
        last=quantity_declaration(lines[-1])
        if len(item_sections)>1 and number is not None and last and last[1]==int(number):
            lines[-1]='订单总数'+str(number)+footer[2]
    return '\n'.join(lines)
