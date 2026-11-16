"""Longest live category evidence before flavor parsing; preserves source text."""
import re
from functools import lru_cache
from catalog_regex import compiled

@lru_cache(maxsize=2048)
def _heading_pattern(alias):
    compact=''.join(c for c in alias if not c.isspace())
    pattern=r'\s*'.join(re.escape(c) for c in compact)
    return re.compile(pattern+r'(?!\s*[一二三四五六七八九十0-9]+代)(?!\s*\d+(?:\.\d+)?\s*[wWkK万])(?=$|口味\s*[:：]|[ \t,:：，;；共总（(*×xX0123456789零一二两三四五六七八九十]|[^\s:：，,]{1,20}仓$)',re.I) if pattern else None


def _heading_match(alias, text):
    # A necessary literal-prefix check avoids compiling unrelated headings.
    # Retain regex Unicode case semantics rather than ASCII/lowercase guesses.
    first=next((c for c in alias if not c.isspace()),'')
    if not first or not compiled(re.escape(first),re.I).match(text):return None
    pattern = _heading_pattern(alias)
    return pattern.match(text) if pattern else None

def _heading_candidates(mappings):
    """Per-prepare necessary-prefix index; original regex remains decisive.

    Preserve mapping order (including equal-length ties) and re.I Unicode
    equivalence. No catalog/rule/customer cache survives this invocation.
    """
    aliases=[(alias,next((c for c in alias if not c.isspace()),'')) for alias in mappings]
    firsts={first:compiled(re.escape(first),re.I) for _,first in aliases if first}
    cache={}
    def candidates(probe):
        if probe in cache:return cache[probe]
        possible={first for first,pattern in firsts.items() if pattern.match(probe)}
        result=[alias for alias,first in aliases if first in possible]
        if len(cache)<64:cache[probe]=result
        return result
    return candidates


def packaging_aliases(mappings):
    return {key:set(values) for key,values in mappings.items()}

def normalize_heading_specs(text,mappings):
    # A terminal packaging descriptor is not another category. Require an
    # exact live/reviewed identity before it; never infer a missing model,
    # shorten a real category, or consume delivery/product detail.
    result=[]
    for line in str(text).splitlines():
        heading=line.strip().rstrip(':：').strip()
        if any(heading.casefold()==str(alias).casefold() for alias in mappings):
            result.append(line);continue
        # Reposition only complete modifiers already present in an audited
        # spelling. Preserve every brand/model/x/capacity character exactly.
        # This is token-order equivalence, not a fuzzy/new category Alias.
        from collections import Counter
        def modifier_key(value):
            value=re.sub(r'\s+','',value).casefold()
            words=re.findall(r'新款|新版|正品',value)
            return re.sub(r'新款|新版|正品','',value),Counter(words)
        core,modifiers=modifier_key(heading)
        if modifiers:
            choices=set().union(*(names for alias,names in mappings.items()
                                   if modifier_key(alias)==(core,modifiers)))
            if len(choices)==1:
                result.append('品类：'+next(iter(choices)));continue
        match=re.fullmatch(r'(.+?)\s*一次性',heading)
        if match:
            base=match[1].strip()
            names=set().union(*(targets for alias,targets in mappings.items()
                               if str(alias).casefold()==base.casefold()))
            if len(names)==1:
                result.append('品类：'+next(iter(names)));continue
        result.append(line)
    return '\n'.join(result)

def _generic_prefix_counted(value):
    """Keep unreviewed flavors as raw scoped refs; do not guess identities."""
    items=[];pos=0
    token=re.compile(r'(\d+)\s*[xX×*]\s*([^\d\s,，、;；()（）]+)')
    while pos<len(value):
        sep=re.match(r'[\s,，、;；]+',value[pos:])
        if sep:pos+=sep.end();continue
        match=token.match(value,pos)
        if not match or int(match.group(1))<=0:return None
        if re.search(r'备选|地址|电话|收件|合计|总计',match.group(2)):return None
        items.append(match.group(2)+'×'+match.group(1));pos=match.end()
    return items or None

def _catalog_counted_list(value, labels):
    """Consume every token using longest scoped identities and local counts.

    Unconsumed text fails closed; a leading section count cannot be spread
    across bare flavours. Digits in a complete catalog label stay opaque.
    """
    ordered=sorted((str(n) for n in labels if n),key=lambda n:(-len(n),n))
    for label in ordered:
        if re.match(r'^\d',label) and '%' not in label:
            identity=r'\s*'.join(re.escape(part) for part in re.findall(r'\d+|[^\d\s]+',label))
            value=re.sub(identity,lambda m:label,value,flags=re.I)
        percentage=re.fullmatch(r'(\d+(?:\.\d+)?)%(.+)',label)
        if percentage:
            level,base=percentage.groups()
            alternate=re.escape(base)+r'\s*(?:[（(]\s*'+re.escape(level)+r'\s*%\s*[）)]|%\s*'+re.escape(level)+r'|'+re.escape(level)+r'\s*%)'
            value=re.sub(alternate+r'\s*(?:[xX×*]\s*(\d+))?(?=$|[\s,，、;；。])',
                         lambda m:label+'×'+(m.group(1) or '1'),value,flags=re.I)
    total=re.search(r'(?:合计|总计|共计|一共|共)\s*(\d+)\s*(?:支|根|个|件)?\s*$',value)
    body=value[:total.start()] if total else value
    pos=0;items=[];prefix_seen=False
    while pos<len(body):
        sep=re.match(r'[\s,，、;；。+＋➕✚✛]+',body[pos:])
        if sep:pos+=sep.end();continue
        prefix=re.match(r'(\d+)\s*(?:[xX×*]\s*)?(个|支|根|件|只)?\s*',body[pos:])
        start=pos+prefix.end() if prefix else pos
        label=next((n for n in ordered if body[start:start+len(n)].casefold()==n.casefold()),None)
        if prefix and label:
            qty=prefix.group(1);pos=start+len(label);prefix_seen=True
        else:
            label=next((n for n in ordered if body[pos:pos+len(n)].casefold()==n.casefold()),None)
            if not label:return _generic_prefix_counted(value)
            end=pos+len(label)
            suffix=re.match(r'\s*[xX×*]?\s*(\d+)\s*(?:个|支|根|件|只)?',body[end:])
            if not suffix:return None
            qty=suffix.group(1);pos=end+suffix.end()
        if int(qty)<=0:return None
        items.append(label+'×'+qty)
    if not items:return None
    if total:items.append('合计'+total.group(1)+'支')
    return items

def _unique_bare_catalog_list(value, labels):
    """Split only a unique complete scoped catalog spelling, never substrings.

    A whole flavor wins over its possible components. Multiple complete
    segmentations, unknown tails and oversized input remain unresolved.
    """
    value=str(value).strip()
    if not value or len(value)>512 or re.search(r'\d|[\s,，、;；()（）]',value):return None
    ordered=sorted({str(n) for n in labels if n},key=lambda n:(-len(n),n))
    if any(value.casefold()==n.casefold() for n in ordered):return None
    paths={0:[()]}
    for pos in range(len(value)):
        for path in paths.get(pos,()):
            for label in ordered:
                if value[pos:pos+len(label)].casefold()!=label.casefold():continue
                end=pos+len(label);bucket=paths.setdefault(end,[])
                candidate=path+(label,)
                if candidate not in bucket and len(bucket)<2:bucket.append(candidate)
    solutions=paths.get(len(value),[])
    return list(solutions[0]) if len(solutions)==1 and len(solutions[0])>1 else None

def _prepare_cache_key(text, products, rows, *, implicit_unknown=None):
    if implicit_unknown is not None:return None
    fields=('id','name','parent_name','parent_id','default_warehouse_id','is_active','is_archived')
    rule_fields=('type','alias','standard','category','status','scope','warehouse_id','category_id','product_id')
    from order_structure import dispatch_reference_prefixes
    return (str(text),tuple(tuple(p.get(k) for k in fields) for p in products),
            tuple(tuple(r.get(k) for k in rule_fields) for r in rows),dispatch_reference_prefixes())

from request_memo import request_memoized

@request_memoized(_prepare_cache_key)
def prepare(text, products, rows, *, implicit_unknown=None):
    # A terminal declared total retains its quantity regardless of the unit.
    text=re.sub(r'((?:共计|一共|合计|总计|共)\s*\d+)\s*颗(?=\s*(?:$|[。.]?\s*$))',r'\1支',str(text),flags=re.M)
    from brand_aliases import scope_matches
    targets={p.get('parent_name') for p in products if p.get('is_active',True)}
    mappings={name:{name} for name in targets if name}
    products_by_category={}
    for product in products:products_by_category.setdefault(product.get('parent_name'),[]).append(product)
    for row in rows:
        if row.get('type')!='MODEL_ALIASES' or row.get('status')!='active':continue
        if row.get('standard') not in targets:continue
        if not any(scope_matches(row,p) for p in products_by_category.get(row['standard'],())):continue
        mappings.setdefault(row['alias'],set()).add(row['standard'])
    # Compose only two independently proven spellings: an audited brand
    # alias and an exact audited canonical-brand generation spelling. Never
    # infer a generation from flavors or extend an unknown generation.
    proven={alias:set(names) for alias,names in mappings.items()}
    # Optional capacity unit only for an already audited exact capacity.
    # "22000" here is model metadata, never a merchandise quantity.
    for alias,names in proven.items():
        if len(names)==1 and re.fullmatch(r'.+?(?:\d{4,6}|\d+(?:\.\d+)?[wW万])口',alias):
            mappings.setdefault(alias[:-1],set()).update(names)
    generation_spellings={}
    for canonical in targets:
        if not canonical:continue
        for spelling,spelling_targets in proven.items():
            if (spelling.startswith(canonical) and len(spelling_targets)==1
                    and re.fullmatch(r'[一二三四五六七八九十0-9]+代',spelling[len(canonical):])):
                generation_spellings.setdefault(canonical,[]).append((spelling[len(canonical):],spelling_targets))
    for alias,names in proven.items():
        if len(names)!=1:continue
        canonical=next(iter(names))
        for suffix,targets_for_spelling in generation_spellings.get(canonical,()):
            composed=alias+suffix
            if composed not in mappings and len(targets_for_spelling)==1:
                mappings[composed]=set(targets_for_spelling)
    heading_candidates=_heading_candidates(mappings)
    # A glued brand/flavor has a boundary only when BOTH sides are exact:
    # live standard or audited brand Alias + complete same-category goods.
    glued=[]
    for line in str(text).splitlines():
        candidate=line.strip()
        for alias,targets_for_alias in sorted(mappings.items(),key=lambda pair:-len(pair[0])):
            if len(targets_for_alias)!=1:continue
            wrapped=next((left+alias+right for left,right in [('(',')'),('（','）'),('[',']'),('【','】')]
                          if candidate.startswith(left+alias+right)),None)
            if wrapped:
                line='品类：'+next(iter(targets_for_alias))+'\n'+candidate[len(wrapped):].lstrip(' :：');break
            if not candidate.startswith(alias):continue
            tail=candidate[len(alias):]
            if not tail or re.match(r'^[\s:：]',tail):continue
            target=next(iter(targets_for_alias))
            flavors={p.get('name') for p in products if p.get('parent_name')==target
                     and p.get('is_active',True) and not p.get('is_archived') and p.get('name')}
            flavors.update(row.get('alias') for row in rows if row.get('type')=='EXPLICIT_FLAVOR'
                and row.get('status')=='active' and row.get('category')==target
                and row.get('standard') in flavors and any(scope_matches(row,p) for p in products if p.get('parent_name')==target))
            counted=_catalog_counted_list(tail,flavors)
            if counted and all(part.rsplit('×',1)[0] in flavors or re.fullmatch(r'合计\d+支',part) for part in counted):
                line='品类：'+target+'\n'+'，'.join(counted);break
        glued.append(line)
    text='\n'.join(glued)
    if re.search(r'[-—–]',str(text)):
        separated=[]
        for line in str(text).splitlines():
            for alias,names in sorted(mappings.items(),key=lambda pair:-len(pair[0])):
                if len(names)!=1 or not line.lstrip().startswith(alias):continue
                tail=line.lstrip()[len(alias):]
                if re.match(r'^\s*[-—–]\s*\S',tail):
                    line='品类：'+next(iter(names))+'\n'+tail.lstrip(' -—–')
                    break
            separated.append(line)
        text='\n'.join(separated)
    text=normalize_heading_specs(text,mappings)
    # A delimiter followed by a proven new heading starts a new scope even
    # on the same physical line. Shield catalog names containing delimiters.
    from order_structure import syntax_text, catalog_names
    protected=catalog_names(products,rows)
    # A star between two words separates list items; a star before a number
    # remains a quantity marker. A complete live catalog identity is opaque.
    split_lines=[]
    for line in str(text).splitlines():
        masked=syntax_text(line,protected)
        cuts=[m.start() for m in re.finditer(r'(?<=[\u4e00-\u9fffA-Za-z])\s*\*(?=\s*[\u4e00-\u9fffA-Za-z])',masked)]
        # Spaces are not identity; replace the actual star at each proven cut.
        stars=[]
        for start in cuts:
            at=line.find('*',start)
            if at>=0:stars.append(at)
        for at in reversed(stars):line=line[:at]+'，'+line[at+1:]
        split_lines.append(line)
    text='\n'.join(split_lines)
    text='\n'.join(re.sub(r'(?<=\d)\s*[✖✕✗✘]\ufe0f?\s*(?=\S)','×',line)
        if not re.search(r'地址|省|市|区|县|街道|楼|栋|室|电话|手机',syntax_text(line,protected))
        else line for line in text.splitlines())
    text=re.sub(r'([*×xX])\s+(?=\d)',r'\1',text)
    # Convert source-proven Chinese unit counts before any default-one list
    # expansion. Otherwise “两根青提，一根菠萝” becomes two fake suffix ×1s.
    from learning_knowledge import _chinese_quantity
    def chinese_units(line):
        masked=syntax_text(line,protected)
        if re.search(r'地址|省|市|区|县|街道|楼|栋|幢|单元|室|电话|手机|1[3-9]\d{9}',masked):
            return line
        matches=list(re.finditer(r'([一二两三四五六七八九十]+)\s*(支|只|根|个|件)',masked))
        for match in reversed(matches):
            quantity=_chinese_quantity(match[1].replace('两','二'))
            if quantity is not None:
                line=line[:match.start()]+str(quantity)+match[2]+line[match.end():]
        # Bare Chinese suffix/prefix counts require an opaque catalog token
        # next to them. Model generations and address numerals stay opaque.
        for pattern in [r'(?<=\ue000)([一二两三四五六七八九十]+)(?=$|[\s,，、;；()（）])',
                        r'(?<!\S)([一二两三四五六七八九十]+)(?=\ue000)']:
            masked=syntax_text(line,protected)
            for match in reversed(list(re.finditer(pattern,masked))):
                quantity=_chinese_quantity(match[1].replace('两','二'))
                if quantity is not None:
                    line=line[:match.start()]+str(quantity)+line[match.end():]
        return line
    text='\n'.join(chinese_units(line) for line in str(text).splitlines())
    # A Chinese sentence stop between counted merchandise is a list separator.
    # Only rewrite when the next token is a proven catalog label/alias; never
    # treat decimal points, address punctuation, or free remarks as quantities.
    labels='|'.join(re.escape(n) for n in sorted(protected,key=lambda n:(-len(n),n)) if n)
    if labels:
        text=re.sub(r'。[ \t]*(?=(?:\d+[ \t]*(?:个|支|根|件)?[ \t]*)?(?:'+labels+r'))','，',str(text))
    text='\n'.join(line for line in str(text).splitlines() if not re.fullmatch(r'\s*(?:口味|味道)\s*[:：]\s*',line))
    from order_structure import fulfillment_parts
    note_lines=[]
    for line in str(text).splitlines():
        directive=fulfillment_parts(line,protected)
        if directive and directive[0] and (re.search(r'备选|备用',line) or directive[1]=='精准发货'):
            primary=directive[0]
            note_lines.extend([primary.rstrip('（( ').strip(),line[len(primary):].strip().strip('（）()')])
        else:note_lines.append(line)
    text='\n'.join(note_lines)
    separated=[]
    for line in str(text).splitlines():
        cuts=[]
        for delimiter in re.finditer(r'[,，;；]',syntax_text(line,protected)):
            remaining=line[delimiter.end():].lstrip()
            hits=[(a,_heading_match(a,remaining)) for a in mappings]
            if any(m and len(mappings[a])==1 and remaining[m.end():].strip(' :：，,;；') for a,m in hits):
                cuts.append(delimiter.start())
        for index in reversed(cuts):line=line[:index]+'\n'+line[index+1:]
        separated.append(line)
    text='\n'.join(separated)
    # Separate glued category/flavor only when the complete suffix is a
    # live flavor (or reviewed scoped Alias) with an explicit count. This
    # does not turn a brand substring in an address into merchandise.
    prepared_lines=[];explicit_subtotal_categories=set()
    for line in str(text).splitlines():
        labelled=re.fullmatch(r'\s*(?:下单产品|下单商品|商品分类|产品分类|商品|产品|款式|型号|品类)\s*[:：]\s*(.+?)\s*',line)
        if labelled:
            heading=labelled.group(1);subtotal_heading=re.fullmatch(
                r'(.+?)\s*[:：]\s*((?:共计|合计|总计|一共|共)\s*\d+\s*(?:个|支|件|颗|盒|瓶|包|套))\s*[:：]?\s*',heading)
            # A catalogue-proven heading and its section subtotal may share
            # a line. Preserve the subtotal exactly once, never as an item.
            category_label=subtotal_heading.group(1).strip() if subtotal_heading else heading
            matches=[names for alias,names in mappings.items() if ''.join(alias.split()).casefold()==''.join(category_label.split()).casefold()]
            choices=set().union(*matches) if matches else set()
            if len(choices)==1:
                line='品类：'+next(iter(choices))
                if subtotal_heading:
                    explicit_subtotal_categories.add(next(iter(choices)))
                    line+='\n'+subtotal_heading.group(2)
        probe=line.strip();candidates=[]
        for alias,names in mappings.items():
            heading_match=_heading_match(alias,probe)
            literal=probe.casefold().startswith(alias.casefold())
            if len(names)!=1 or not (heading_match or literal):continue
            category=next(iter(names));tail=probe[heading_match.end() if heading_match else len(alias):]
            pool=[p for p in products if p.get('parent_name')==category and p.get('is_active',True) and not p.get('is_archived')]
            labels={p['name'] for p in pool}
            labels.update(r.get('alias') for r in rows if r.get('type')=='EXPLICIT_FLAVOR'
                and r.get('status')=='active' and r.get('category')==category
                and any(scope_matches(r,p) and r.get('standard')==p['name'] for p in pool))
            body=tail.strip()
            if body in labels and tail and not tail[0].isspace() and tail[0] not in ':：':
                candidates.append((len(alias),category,body+'×1'))
            parenthesized_single=re.fullmatch(r'[:：]?\s*[xX×*]?\s*(\d+)\s*(?:个|支|只|根|件)?\s*[（(]([^()（）,，、;；]+)[）)]',body)
            if parenthesized_single and parenthesized_single.group(2).strip() in labels:
                candidates.append((len(alias),category,parenthesized_single.group(2).strip()+'×'+parenthesized_single.group(1)))
            counted_body=body.lstrip(':：，,;；').strip()
            counted_body=re.sub(r'[（(]\s*((?:共计|共|合计|总计)\s*\d+\s*(?:支|只|根|个|件))\s*[）)]\s*$',r'，\1',counted_body)
            counted_list=_catalog_counted_list(counted_body,labels)
            if counted_list:
                candidates.append((len(alias),category,'\n'.join(counted_list)))
            bare_list=_unique_bare_catalog_list(body.lstrip(':：').strip(),labels)
            if bare_list:
                candidates.append((len(alias),category,'\n'.join(n+'×1' for n in bare_list)))
            # A reviewed heading may touch a complete compact item list.
            # Preserve unknown flavours as explicit scoped items so normal
            # catalogue validation rejects them instead of dropping the list.
            if tail and not tail[0].isspace() and tail[0] not in ':：，,':
                total_match=re.search(r'(?:合计|总计|共计|一共|共)\s*(\d+)\s*(?:支|根|个|件)?\s*$',body)
                goods=body[:total_match.start()] if total_match else body
                compact=re.fullmatch(r'(?:[^\d\s,，:：;；()（）]+?[xX×*]?\d+(?:支|根|个|件)?\s*){2,}',goods)
                # Reject unresolved generation/capacity suffixes as headings.
                if compact and not re.match(r'^(?:[一二三四五六七八九\d]+代|\d)',goods):
                    parts=re.findall(r'([^\d\s,，:：;；()（）]+?)[xX×*]?(\d+)(?:支|根|个|件)?\s*',goods)
                    if len(parts)>1:
                        expanded='\n'.join(name+'×'+qty for name,qty in parts)
                        if total_match:expanded+='\n合计'+total_match.group(1)+'支'
                        candidates.append((len(alias),category,expanded))
            enumerated=re.fullmatch(r'(\d+)\s*(?:个|支|件|颗|盒|瓶|包|套)\s*[（(]([^()（）]+)[）)]',body)
            if enumerated:
                parts=[part.strip() for part in re.split(r'[/／、,，]',enumerated.group(2))]
                if (len(parts)==int(enumerated.group(1)) and len(parts)>1 and all(part in labels for part in parts)):
                    candidates.append((len(alias),category,'\n'.join(part+'×1' for part in parts)+'\n小计'+enumerated.group(1)+'支'))
            from order_structure import fulfillment_parts, catalog_names
            directive=fulfillment_parts(body,catalog_names(products,rows))
            primary=directive[0] if directive else body
            counted=re.fullmatch(r'[xX×*]\s*(\d+)(?:\s+(.+)|([^\d\s].*))',primary)
            counted_flavor=(counted.group(2) or counted.group(3)).strip() if counted else ''
            # A proven category may touch its count and a single flavor.
            # Preserve unknown scoped tokens for the existing confirmation
            # gate too; quantity syntax is never part of a learned alias.
            single_unknown=(_generic_prefix_counted(counted.group(1)+'×'+counted_flavor)
                            if counted else None)
            if (counted and int(counted.group(1))>0
                    and (counted_flavor in labels or single_unknown)):
                # A one-flavor category subtotal is conserved, not a product
                # named after the brand. Keep backup instructions separate.
                suffix=body[len(primary):].strip() if directive else ''
                candidates.append((len(alias),category,counted_flavor+'×'+counted.group(1)
                                   + ('\n'+suffix if suffix else '')))
            wrapped=re.fullmatch(r'[{｛]([^{}｛｝]+)[}｝]\s*([xX×*]?\s*\d+\s*(?:支|根|个|件)?)',body)
            if wrapped and wrapped.group(1).strip() in labels:
                candidates.append((len(alias),category,wrapped.group(1).strip()+wrapped.group(2)))
            elif (tail and not tail[0].isspace() and tail[0] not in ':：'
                  and any(re.fullmatch(re.escape(label)+r'\s*[xX×*]?\s*\d+\s*(?:支|根|个|件)?',body,re.I) for label in labels if label)):
                candidates.append((len(alias),category,body))
        if candidates:
            longest=max(c[0] for c in candidates);hits={c[1:] for c in candidates if c[0]==longest}
            if len(hits)==1:
                category,body=next(iter(hits));prepared_lines.extend(('品类：'+category,body));continue
        prepared_lines.append(line)
    text='\n'.join(prepared_lines)
    result=[]; subtotal=None; categories_seen=0; current_category=None
    def unquantified_known_list(value,category):
        """Conserve a whitespace/punctuation-delimited list under an explicit brand."""
        pool=[p for p in products if p.get('parent_name')==category and p.get('is_active',True)]
        labels={str(p.get('name') or '') for p in pool if p.get('name')}
        labels.update(str(row.get('alias') or '') for row in rows
            if row.get('type')=='EXPLICIT_FLAVOR' and row.get('status')=='active'
            and row.get('category')==category and any(scope_matches(row,p) and row.get('standard')==p.get('name') for p in pool))
        ordered=sorted(labels,key=lambda item:(-len(item),item))
        value=value.strip().strip('。.')
        if not value or re.search(r'\d|共|合计|备选|备用|随机|地址|电话|手机|[()（）]',value):return None
        bare_list=_unique_bare_catalog_list(value,labels)
        if bare_list:return bare_list
        parts=[];pos=0
        while pos<len(value):
            sep=re.match(r'[\s,，、;；+＋➕✚✛]+',value[pos:])
            if sep:pos+=sep.end();continue
            matches=[label for label in ordered if value[pos:pos+len(label)].casefold()==label.casefold()
                     and (pos+len(label)==len(value) or re.match(r'[\s,，、;；+＋➕✚✛]',value[pos+len(label):]))]
            if not matches:
                # A delimited bare list under a proven heading is still a
                # list when one flavour is unknown. Preserve every entry;
                # catalogue resolution must request confirmation separately.
                parts=[p.strip() for p in re.split(r'[,，、;；+＋➕✚✛]|\s+',value) if p.strip()]
                if len(parts)<2 or any(re.search(r'收件|收货|姓名|联系人|省|市|区|县|路|街|楼|栋|室|仓|发货|备注|谢谢|尽快',p) for p in parts):
                    return None
                if implicit_unknown is not None:
                    approved={label.casefold() for label in labels}
                    for part in parts:
                        if part.casefold() not in approved:
                            ref='品类('+category+')口味('+part+')'
                            if ref not in implicit_unknown:implicit_unknown.append(ref)
                return parts
            chosen=matches[0];parts.append(chosen);pos+=len(chosen)
        return parts if len(parts)>1 else None
    def declared_list_lines(tail, category):
        """Expand an explicit each-count or conserved one-each enumeration.

        Catalog labels are opaque; unknown tokens remain explicit items and
        must fail normal matching, never disappear behind a matched flavor.
        """
        leading_total=re.match(r'^\s*(\d+)\s*(?:支|只|根|个|件)\s+(?=\S)',tail)
        if not leading_total and not re.search(r'各|共|总计|合计',tail):return None
        from order_structure import _number, syntax_text
        from brand_aliases import scope_matches
        pool=[p for p in products if p.get('parent_name')==category and p.get('is_active',True)]
        known={str(p['name']) for p in pool}
        known.update(str(row.get('alias') or '') for row in rows
            if row.get('type')=='EXPLICIT_FLAVOR' and row.get('status')=='active'
            and row.get('category')==category and any(scope_matches(row,p) and row.get('standard')==p['name'] for p in pool))
        body=tail.strip().rstrip('。.').strip()
        if leading_total:body=body[leading_total.end():].strip()
        body=re.sub(r'\s*(?:不要发错|别发错|精准发货)\s*[。.]?\s*$','',body)
        # Detect the terminal section total on the original text first.  The
        # syntax adapter deliberately replaces catalogue labels with opaque
        # placeholders; when a customer glues ``共3`` to the final flavour
        # (``甲味，乙味，丙味共3根``), slicing with offsets from the adapted
        # string corrupts the source and can invent a flavour named ``丙味共``.
        raw_total=re.search(r'(?:共计|一共|共|总计|合计)\s*(\d+|[零一二两三四五六七八九十]{1,3})\s*(?:支|只|根|个|件)?\s*$',body)
        probe=syntax_text(body,known)
        total=raw_total or re.search(r'(?:共计|一共|共|总计|合计)\s*(\d+|[零一二两三四五六七八九十]{1,3})\s*(?:支|只|根|个|件)?\s*$',probe)
        total_qty=None
        if total:
            numeral=total.group(1)
            total_qty=int(numeral) if numeral.isdigit() else int(re.sub(r'\D','',re.sub(r'([零一二两三四五六七八九十]{1,3})(个)',_number,numeral+'个')))
            body=body[:total.start()].rstrip(' ,，、;；');probe=syntax_text(body,known)
        if leading_total:
            leading_qty=int(leading_total.group(1))
            if total_qty is not None and total_qty!=leading_qty:
                return ['[并列口味数量冲突]',body,'小计'+str(leading_qty)+'支','小计'+str(total_qty)+'支']
            total_qty=leading_qty
        each=re.search(r'各\s*(\d+|[零一二两三四五六七八九十]{1,3})\s*(?:支|只|根|个|件)?\s*$',probe)
        if each:
            numeral=each.group(1)
            each_qty=(int(numeral) if numeral.isdigit() else
                      int(re.sub(r'\D','',re.sub(
                          r'([零一二两三四五六七八九十]{1,3})(个)',_number,numeral+'个'))))
            body=body[:each.start()].rstrip(' ,，、;；')
        else:each_qty=None
        if total_qty is None and each_qty is None:return None
        if re.search(r'备选|备用|随机|缺货|[()（）]|地址|电话|手机',syntax_text(body,known)):return None
        body=re.sub(r'^一次性\s+','',body)
        # Preserve multiword real labels and aliases before whitespace split.
        label_pattern='|'.join(re.escape(n) for n in sorted(known,key=lambda n:(-len(n),n)) if n)
        token=re.compile('(?:'+label_pattern+r')(?=$|[\s,，、;；+＋])',re.I) if label_pattern else None
        parts=[];pos=0
        while pos<len(body):
            sep=re.match(r'[\s,，、;；+＋]+',body[pos:])
            if sep:pos+=sep.end();continue
            match=token.match(body,pos) if token else None
            if match:parts.append(match.group());pos=match.end();continue
            unknown=re.match(r'[^\s,，、;；+＋]+',body[pos:])
            if not unknown:return None
            parts.append(unknown.group());pos+=unknown.end()
        if len(parts)==1 and total_qty is not None and each_qty is None:
            # One catalog-proven flavor plus a declared count has no
            # allocation ambiguity. Multiple bare flavors still default to
            # one each and retain their independent subtotal validation.
            if parts[0].casefold() in {str(n).casefold() for n in known}:
                return [parts[0]+'×'+str(total_qty),'小计'+str(total_qty)+'支']
        if len(parts)<2:return None
        # Explicit per-item numbers belong to the existing tokenizer, not to
        # this one-each inference; numeric catalog labels remain protected.
        if any(re.search(r'\d|[零一二两三四五六七八九十]+\s*(?:支|只|根|个|件)',syntax_text(part,known)) for part in parts):return None
        if each_qty is None:
            # Confirmed business rule: every listed flavour without its own
            # quantity means one unit. Keep the declared subtotal as an
            # independent section check so a mismatch still fails closed.
            return [part+'×1' for part in parts] + ['小计'+str(total_qty)+'支']
        if each_qty<=0:return None
        expanded=[part+'×'+str(each_qty) for part in parts]
        if total_qty is not None:expanded.append('小计'+str(total_qty)+'支')
        return expanded
    def implicit_lines(tail):
        parts=[part.strip().strip('。.') for part in re.split(r'[,，、;；+＋]',tail) if part.strip().strip('。.')]
        if len(parts)>1 and all(not re.search(r'\d|[零一二两三四五六七八九十]+\s*(?:根|支|个|件)|共|合计|备选|备用|随机|地址|电话|手机|[()（）]',part) for part in parts):
            return [part+'×1' for part in parts]
        return None
    # A trailing declaration is its own role, never a flavor called "共".
    # Restrict this split to a delimited terminal count, not address numbers,
    # model capacities or arithmetic. Source text remains untouched upstream.
    input_lines=[]
    from order_structure import catalog_names, syntax_text
    protected=catalog_names(products,rows)
    raw_lines=str(text).splitlines();source_index=0
    # A leading unit count belongs to the explicitly named category, not to
    # each following flavor. Move it only across a proven catalog heading.
    for index, line in enumerate(raw_lines):
        prefix=re.match(r'^\s*(\d+)\s*(?:支|只|根|个|件)\s*(.+)$',line)
        if not prefix:continue
        body=prefix.group(2)
        hits=[(alias,_heading_match(alias,body)) for alias in heading_candidates(body)]
        hits=[(alias,match) for alias,match in hits if match]
        if not hits:continue
        alias,match=max(hits,key=lambda pair:len(re.sub(r'\s+','',pair[0])))
        if len(mappings[alias])==1:
            raw_lines[index]=body[:match.end()]+' '+prefix.group(1)+'个 '+body[match.end():].lstrip(' :：，,')
    while source_index<len(raw_lines):
        source_line=raw_lines[source_index];source_index+=1
        probe=source_line.lstrip()
        matches=[(alias,_heading_match(alias,probe)) for alias in heading_candidates(probe)]
        matches=[(a,m) for a,m in matches if m]
        if matches:
            alias,matched=max(matches,key=lambda pair:len(re.sub(r'\s+','',pair[0])))
            names=mappings[alias]
            if len(names)==1:
                category=next(iter(names));tail=probe[matched.end():].lstrip(' :：，,')
                # A customer routing label may be written directly after a
                # proven category Alias (for example “鸭子蔬菜仓”).  It is an
                # auxiliary warehouse note, never a fifth flavor in the
                # following “各一” enumeration.
                auxiliary_tail=(tail if re.fullmatch(r'[^\s,，、;；:：]{1,20}仓',tail) else '')
                goods_tail='' if auxiliary_tail else tail
                expanded=declared_list_lines(goods_tail,category);end=source_index
                # A bounded multiline bare-flavor enumeration can carry its
                # count on the final line. Never cross a contact/new category
                # or an already quantified item to discover a later total.
                joined=goods_tail
                if not expanded and not re.search(r'\d',syntax_text(tail,protected)):
                    for next_index in range(source_index,min(len(raw_lines),source_index+32)):
                        later=raw_lines[next_index].strip()
                        if (not later or re.search(r'1[3-9]\d{9}|收件|收货|姓名|地址|电话|手机|备选|备用',later)
                                or any(_heading_match(a,later) for a in mappings)):
                            break
                        joined+='\n'+later
                        expanded=declared_list_lines(joined,category)
                        if expanded:end=next_index+1;break
                        if re.search(r'\d',syntax_text(later,protected)):break
                if expanded:
                    input_lines.extend(['品类：'+category]+expanded)
                    if auxiliary_tail:input_lines.append(auxiliary_tail)
                    source_index=end;continue
        sentences=[part.strip() for part in source_line.split('。') if part.strip()]
        if (len(sentences)>1 and any(match and match.end()==len(sentences[0])
                for alias in heading_candidates(sentences[0]) for match in [_heading_match(alias,sentences[0])])
                and re.search(r'\d',sentences[1])):
            input_lines.extend(sentences)
            continue
        syntax=syntax_text(source_line,protected)
        total=re.search(r'(?:共计|共|合计|总计|一共)\s*\d+\s*(?:支|只|根|个|件)?\s*$',syntax)
        prefix=source_line[:total.start()].strip() if total else ''
        prefix_probe=re.sub(r'^\s*(?:正品\s*)+', '', prefix).strip()
        only_heading=any(match and match.end()==len(probe)
                         for probe in (prefix,prefix_probe) for alias in heading_candidates(probe)
                         for match in [_heading_match(alias,probe)])
        anchored=(any(_heading_match(alias,probe)
                      for probe in (source_line.strip(),re.sub(r'^\s*(?:正品\s*)+', '', source_line).strip())
                      for alias in heading_candidates(probe))
                  or any(compiled(re.escape(str(p.get('name') or ''))+r'(?=$|[\s,，、/／×xX*\d])').match(source_line.strip())
                         for p in products if p.get('name') and p.get('is_active',True)))
        if (total and total.start()>0 and '\ue000' in syntax[:total.start()]
                and anchored and not only_heading
                and not re.search(r'1[3-9]\d{9}|收件|收货|姓名|地址|省|市|区|县|街道',syntax[:total.start()])):
            input_lines.extend([source_line[:total.start()].rstrip(' ,，'),source_line[total.start():]])
        else:input_lines.append(source_line)
    for line_index,line in enumerate(input_lines):
        pure_directive=fulfillment_parts(line,protected)
        if pure_directive and not pure_directive[0].strip(' ()（）,，;；'):
            result.append(line);continue
        matches=[]
        # An approved Alias may itself contain the modifier. Match the
        # complete original first; only then try an optional leading modifier.
        for probe in dict.fromkeys((line.lstrip(),re.sub(r'^\s*(?:正品\s*)+', '', line).lstrip(),
                re.sub(r'^\s*(?:品类|型号)\s*[:：]\s*','',line).lstrip())):
            matches=[alias for alias in heading_candidates(probe) if _heading_match(alias,probe)]
            if matches:break
        if not matches:
            # A new, unreviewed capacity/model heading is a scope boundary,
            # not two flavors inherited from the previous category. Opaque
            # catalog labels win; preserve the unknown model for rejection.
            masked=syntax_text(line,protected)
            unknown_model=re.match(r'^\s*([^\s:：,，\ue000]+?\d+(?:\.\d+)?\s*(?:万|[wWkK])(?:口)?)\s*[:：]?\s+(.+)$',masked)
            if unknown_model and not re.search(r'地址|省|市|区|县|电话|手机|楼|栋|室',masked):
                if subtotal is not None:
                    result.append('小计'+subtotal+'支');subtotal=None
                boundary=unknown_model.end(1)
                current_category=line[:boundary].strip()
                result.extend(['品类：'+current_category,line[boundary:].lstrip(' :：')])
                continue
            if current_category:
                pool=[p for p in products if p.get('parent_name')==current_category and p.get('is_active',True) and not p.get('is_archived')]
                labels={p['name'] for p in pool}
                labels.update(r.get('alias') for r in rows if r.get('type')=='EXPLICIT_FLAVOR'
                    and r.get('status')=='active' and r.get('category')==current_category
                    and any(scope_matches(r,p) and r.get('standard')==p['name'] for p in pool))
                counted_list=_catalog_counted_list(line,labels)
                if counted_list:
                    result.extend(counted_list);continue
                bare_list=_unique_bare_catalog_list(line,labels)
                if bare_list:
                    result.extend(n+'×1' for n in bare_list);continue
            # Parentheses can wrap an explicitly counted flavor, not just a
            # prose note. Unwrap only a flavor proven in the current category;
            # never guess a product or remove arbitrary customer parentheses.
            wrapped = re.fullmatch(r'\s*[（(]([^()（）]+)[）)]\s*(\d+)\s*(?:枝|支|根|个|件)\s*', line)
            if current_category and wrapped:
                from brand_aliases import scope_matches
                pool = [p for p in products if p.get('parent_name') == current_category and p.get('is_active', True)]
                known = {str(p['name']).casefold() for p in pool}
                known.update(str(row.get('alias') or '').casefold() for row in rows
                    if row.get('type') == 'EXPLICIT_FLAVOR' and row.get('status') == 'active'
                    and row.get('category') == current_category
                    and any(scope_matches(row, p) and row.get('standard') == p['name'] for p in pool))
                if wrapped.group(1).strip().casefold() in known and int(wrapped.group(2)) > 0:
                    result.append(wrapped.group(1).strip() + '×' + wrapped.group(2))
                    continue
            combined_note=re.fullmatch(r'\s*(.+?)\s*([*×xX]?\s*\d+)\s+精准发货\s+(备选.+)\s*',line)
            if current_category and combined_note:
                pool=[p for p in products if p.get('parent_name')==current_category and p.get('is_active',True) and not p.get('is_archived')]
                labels={p['name'] for p in pool}
                labels.update(r.get('alias') for r in rows if r.get('type')=='EXPLICIT_FLAVOR'
                    and r.get('status')=='active' and r.get('category')==current_category
                    and any(scope_matches(r,p) and r.get('standard')==p['name'] for p in pool))
                if combined_note.group(1).strip() in labels:
                    # Keep both dispatch instructions as evidence, separately
                    # from an exact catalogue-backed purchased item/count.
                    result.extend((combined_note.group(1).strip()+combined_note.group(2),'精准发货',combined_note.group(3)))
                    continue
            scoped=re.fullmatch(r'\s*(?:共|合计|共计)\s*(\d+)\s*(?:支|根|个|件)\s*',line)
            # A category already declares its subtotal in the heading. A
            # different final "共N" after multiple headings is the order
            # total, not a second contradictory subtotal for the last brand.
            later_header=False
            if subtotal is not None and categories_seen>1 and scoped:
                later_header=any(compiled(r'^\s*(?:正品\s*)?'+re.escape(alias)+
                    r'(?=$|[\s:：共总（(0-9零一二两三四五六七八九十])',re.I).match(later)
                    for later in input_lines[line_index+1:] for alias in mappings)
            explicit_order_total=(subtotal is not None and categories_seen>1 and scoped
                                  and not later_header and int(scoped.group(1))!=int(subtotal))
            if subtotal is not None and (explicit_order_total or re.match(r'^\s*(?:总数量|总计|总共|一共|订单总数)',line)):
                result.append('小计'+subtotal+'支');subtotal=None
            if explicit_order_total:line='总计'+scoped.group(1)+'支'
            if current_category and re.search(r'\d',line):
                pool=[p for p in products if p.get('parent_name')==current_category and p.get('is_active',True) and not p.get('is_archived')]
                labels={str(p['name']).casefold() for p in pool}
                labels.update(str(row.get('alias','')).casefold() for row in rows
                    if row.get('type')=='EXPLICIT_FLAVOR' and row.get('status')=='active'
                    and row.get('category')==current_category and any(scope_matches(row,p) and row.get('standard')==p['name'] for p in pool))
                parts=[p.strip() for p in re.split(r'[,，、;；]',line) if p.strip()]
                if len(parts)>1 and all(p.casefold() in labels for p in parts):
                    result.extend('品类('+current_category+')口味('+p+')×1' for p in parts)
                    continue
            if current_category and not re.search(r'\d|地址|电话|手机|省|市|区|县|仓',line):
                pool=[p for p in products if p.get('parent_name')==current_category and p.get('is_active',True)]
                known={str(p['name']).casefold() for p in pool}
                known.update(str(row.get('alias') or '').casefold() for row in rows
                    if row.get('type')=='EXPLICIT_FLAVOR' and row.get('status')=='active'
                    and row.get('category')==current_category and any(scope_matches(row,p) and row.get('standard')==p['name'] for p in pool))
                from order_structure import fulfillment_parts, catalog_names
                directive=fulfillment_parts(line,catalog_names(products,rows))
                preferred=directive[0] if directive else line
                parts=unquantified_known_list(preferred,current_category) or []
                if len(parts)>1:
                    result.extend(p+'×1' for p in parts)
                    if directive:
                        result.append(line[len(directive[0]):].strip())
                    continue
                flavor_line=line.strip().strip(',，、;；。.').strip()
                flavor_line=re.sub(r'^(?:口味|味道)\s*[:：]\s*','',flavor_line)
                if flavor_line.casefold() in known:
                    result.append(flavor_line+'×1');continue
            # A standalone warehouse/routing note after a category subtotal
            # is not a flavor enumeration. Feeding it to
            # ``declared_list_lines`` invents a subtotal/list conflict and can
            # make a correct ``4 + (2,1,1)`` order look like eight units.
            from order_structure import _WAREHOUSE
            if _WAREHOUSE.fullmatch(line):
                result.append(line)
                continue
            # A category heading may declare its count on the previous line.
            # Bind only an immediately following conserved flavor enumeration;
            # do not reinterpret that category subtotal as an order total.
            declared_line=(subtotal+'个 '+line) if subtotal is not None else line
            expanded=declared_list_lines(declared_line,current_category) if current_category else None
            if expanded:
                result.extend(expanded);subtotal=None;continue
            result.append(line);continue
        longest=max(matches,key=lambda x:len(re.sub(r'\s+','',x)));names=mappings[longest]
        if len(names)!=1:result.append(line);continue
        categories_seen+=1
        current_category=next(iter(names))
        if subtotal is not None:
            result.append('小计'+subtotal+'支');subtotal=None
        matched_end=_heading_match(longest,probe).end()
        tail=probe[matched_end:].strip().lstrip(':：，,;；').strip()
        tail=re.sub(r'^口味\s*[:：]\s*','',tail)
        # An audited category followed by xN: declares the section count.
        # Keep X-containing catalog model identities opaque (longest heading
        # already won); never generate a flavor named "x" from this syntax.
        # A catalog-resolved model followed by N:flavors or N(flavors)
        # declares a section count, not a flavor containing "N:". Delimited
        # wording is required; bare model digits remain opaque identities.
        delimited_count=re.fullmatch(r'([1-9]\d*)\s*(?:[:：]\s*(.+)|[（(]([^()（）]+)[）)])',tail)
        if delimited_count:
            body=(delimited_count.group(2) or delimited_count.group(3)).strip()
            pool={str(p.get('name') or '') for p in products
                  if p.get('parent_name')==current_category and p.get('is_active',True) and not p.get('is_archived')}
            live_pool=[p for p in products if p.get('parent_name')==current_category
                       and p.get('is_active',True) and not p.get('is_archived')]
            pool.update(str(row.get('alias') or '') for row in rows
                if row.get('type')=='EXPLICIT_FLAVOR' and row.get('status')=='active'
                and row.get('category')==current_category
                and any(scope_matches(row,p) and row.get('standard')==p.get('name') for p in live_pool))
            normalized_body=(body+'×1' if body in pool else body)
            result.extend(['品类：'+current_category,normalized_body,'小计'+delimited_count.group(1)+'支'])
            continue
        counted_heading=re.fullmatch(r'[*×xX]\s*([1-9]\d*)\s*[:：]\s*(.+)',tail)
        if counted_heading:
            result.extend(['品类：'+current_category,counted_heading.group(2),
                           '小计'+counted_heading.group(1)+'支'])
            continue
        labelled_count=re.fullmatch(r'(\d+)\s*(?:支|只|根|个|件)\s*(?:口味|味道)\s*[:：]\s*(.+)',tail)
        if labelled_count:
            # A category's count before an explicit flavor field is a
            # section declaration, never a flavor named "口味".
            result.extend(['品类：'+current_category,labelled_count.group(2),
                           '小计'+labelled_count.group(1)+'支'])
            continue
        if re.match(r'^\d+\s*(?:支|只|根|个|件)\s*',tail):
            # A counted flavor list owns its leading number. Never steal
            # the first item's quantity as a category subtotal.
            pool={str(p.get('name') or '') for p in products
                  if p.get('parent_name')==current_category and p.get('is_active',True)}
            candidate=re.sub(r'[（(]\s*((?:共计|共|合计|总计)\s*\d+\s*(?:支|只|根|个|件))\s*[）)]\s*$',r'，\1',tail)
            counted=_catalog_counted_list(candidate,pool)
            if counted:
                result.extend(['品类：'+current_category]+counted)
                continue
        # A delimiter inside one exact live catalog label is opaque. Adding
        # semicolon heading support must not split such a name into flavors.
        opaque=[str(p.get('name') or '') for p in products if p.get('parent_name')==current_category
                and p.get('is_active',True) and not p.get('is_archived') and ';' in str(p.get('name') or '')]
        if any(re.fullmatch(re.escape(label)+r'\s*(?:[×xX*]\s*)?\d+\s*(?:支|根|个|件)?',tail,re.I) for label in opaque):
            result.extend(['品类：'+current_category,tail])
            continue
        expanded=declared_list_lines(tail,current_category)
        if expanded:
            result.extend(['品类：'+current_category]+expanded)
            continue
        arbitrary_flavors=re.fullmatch(r'[*×xX]?\s*(\d+)\s*(?:支|根|个|件)?\s*[,，]?\s*(?:味道|口味)\s*随意',tail)
        if arbitrary_flavors:
            result.extend(['品类：'+current_category,'随机'+arbitrary_flavors.group(1)+'个口味'])
            continue
        wrapped_prefix=re.fullmatch(r'[（(]([^()（）]+)[）)]',tail)
        if wrapped_prefix:
            parts=re.split(r'[\s,，、;；]+',wrapped_prefix.group(1).strip())
            prefix_items=[re.fullmatch(r'(\d{1,3})\s*(?:支|根|个|件)?([^\d]+)',part) for part in parts]
            pool=[p for p in products if p.get('parent_name')==current_category and p.get('is_active',True)]
            known={str(p['name']).casefold() for p in pool}
            known.update(str(row.get('alias') or '').casefold() for row in rows
                if row.get('type')=='EXPLICIT_FLAVOR' and row.get('status')=='active'
                and row.get('category')==current_category
                and any(scope_matches(row,p) and row.get('standard')==p['name'] for p in pool))
            if (len(parts)>1 and all(match and int(match.group(1))>0
                    and match.group(2).strip().casefold() in known for match in prefix_items)):
                result.append('品类：'+current_category)
                result.extend(match.group(2).strip()+'×'+match.group(1) for match in prefix_items)
                continue
        primary_random = re.fullmatch(r'(?:随机|随便)\s*(\d+)\s*(?:支|根|个(?:口味)?)?', tail)
        if primary_random:
            result.extend(['品类：'+current_category, '随机'+primary_random.group(1)+'个口味'])
            continue
        from order_structure import _WAREHOUSE
        if _WAREHOUSE.fullmatch(tail):
            result.extend(['品类：'+current_category,tail])
            continue
        multiplied=re.fullmatch(r'[*×xX]\s*(\d+)\s*(?:支|根|个|件)?\s*[:：]?',tail)
        if multiplied:
            subtotal=multiplied.group(1)
            result.append(next(iter(names)))
            continue
        count=re.match(r'^([0-9]+|[零一二两三四五六七八九十]+)\s*(支|只|根|个|件)\s*(?:[:：]\s*)?(.*)$',tail)
        # A count immediately followed by one flavor is that item's quantity,
        # not a category declaration. A plural list can still carry a subtotal.
        body=count.group(3).strip() if count else ''
        wrapped_body=re.fullmatch(r'[（(]([^()（）]+)[）)]',body)
        if wrapped_body:body=wrapped_body.group(1).strip()
        explicit_list=(len(re.findall(r'\d+\s*(?:支|只|根|个|件)?',body))>1)
        if count and body and not implicit_lines(body) and not wrapped_body and not explicit_list:
            known={str(p.get('name') or '').casefold() for p in products
                   if p.get('parent_name')==current_category and p.get('is_active',True)}
            result.extend(['品类：'+current_category,
                body+'×'+count.group(1) if body.casefold() in known else tail])
            continue
        if count:
            from order_structure import _number
            subtotal=count.group(1) if count.group(1).isdigit() else re.match(r'\d+',_number(count)).group()
            result.append(next(iter(names)))
            if body:
                result.extend(implicit_lines(body) or [body+'×1' if wrapped_body and not re.search(r'\d',body) else body])
            continue
        if re.fullmatch(r'(?:共计|共|合计|总计)\s*\d+\s*(?:支|根|个|件)',tail):
            result.extend(['品类：'+next(iter(names)),tail])
        else:
            standard=next(iter(names))
            # A live identity containing digits is a heading, never a count.
            # Explicitly mark it before the legacy quantity adapter runs.
            heading='品类：'+standard
            # Mixed explicit counts and an uncounted flavour must preserve
            # every token.  The confirmed business rule is that an omitted
            # flavour quantity defaults to one; unknown spellings still flow
            # through normal catalogue validation and cannot disappear.
            live_pool=[p for p in products
                       if p.get('parent_name')==current_category and p.get('is_active',True)]
            pool=[str(p.get('name') or '') for p in live_pool]
            # A reviewed flavor Alias is an equally valid opaque label here.
            # Without it, ``葡萄 ×1`` under the real 冰爆 catalog is split
            # into ``葡萄`` and ``×1`` because only ``巨峰葡萄`` is a canonical
            # name; the first explicit unit then becomes "missing" and the
            # section total silently falls from three to two.
            pool.extend(str(row.get('alias') or '') for row in rows
                if row.get('type')=='EXPLICIT_FLAVOR' and row.get('status')=='active'
                and row.get('category')==current_category
                and any(scope_matches(row,p) and row.get('standard')==p.get('name') for p in live_pool))
            pool=list(dict.fromkeys(name for name in pool if name))
            bare_tail=tail.strip().strip('，,、;；。.').strip()
            if bare_tail.casefold() in {name.casefold() for name in pool}:
                result.extend([heading,bare_tail+'×1'])
                continue
            for name in pool:
                if re.match(r'^\d',name):
                    identity=r'\s*'.join(re.escape(part) for part in re.findall(r'\d+|[^\d\s]+',name))
                    tail=re.sub(r'(?<!\w)'+identity+r'(?=\s*(?:[xX×*]?\s*\d|[,，、;；]|$))',lambda m:name,tail)
                    tail=re.sub(re.escape(name)+r'\s+(?=\d+\s*(?:支|只|根|个|件)?(?:[,，、;；]|$))',lambda m:name,tail)
            pattern='|'.join(re.escape(n) for n in sorted(pool,key=lambda n:-len(n)) if n)
            # A spaced multiplication marker belongs to the preceding exact
            # live flavor. Do not turn "西瓜冰 x1" into two list entries.
            # If a marker itself is a real flavor, preserve the ambiguity.
            if pattern and not any(n.casefold() in {'x','×','*'} for n in pool):
                tail=re.sub('('+pattern+r')\s+([xX×*])\s*(?=\d)',r'\1\2',tail,flags=re.I)
                # Counts separated by spaces are still attached to the exact
                # catalog flavor. Split only after binding them, otherwise
                # "芦荟甘泉 1，100 号1" falls through the legacy tokenizer.
                tail=re.sub('('+pattern+r')\s+(\d+)\s*((?:支|只|根|个|件)?)(?=$|[\s,，、;；])',
                            r'\1×\2\3',tail,flags=re.I)
            tokens=[part for part in re.split(r'[\s,，、;；]+',tail) if part]
            parts=[re.fullmatch('('+pattern+r')(?:[xX×*]?\s*(\d+)\s*(?:支|只|根|个|件)?)?',part,re.I)
                   for part in tokens] if pattern else []
            # Preserve unknown/abbreviated flavors too: an unreviewed spelling
            # must not let a mixed-count list bypass coverage altogether.
            from order_structure import syntax_text, catalog_names
            if len(tokens)>1 and not re.search(r'备选|备用|缺货|没货|没有|随机|地址|电话|手机|共|合计',
                                               syntax_text(tail,catalog_names(products,rows))):
                parts=[match or re.fullmatch(r'([^\d]+?)(?:[xX×*]?(\d+)\s*(?:支|只|根|个|件)?)?',part)
                       for match,part in zip(parts,tokens)] if parts else []
            if len(parts)>1 and all(parts) and any(part.group(2) for part in parts):
                result.append('品类：'+current_category)
                for part in parts:
                    result.append(part.group(1)+'×'+part.group(2) if part.group(2)
                                  else part.group(1)+'×1')
                continue
            # Confirmed business rule: under a proven live category, every
            # unquantified listed flavour defaults to one unit.
            implicit=unquantified_known_list(tail,current_category) or []
            if (len(implicit)>1 and all(not re.search(r'\d|共|合计|备选|备用|随机|地址|电话|手机|[()（）]',part)
                                       for part in implicit)):
                result.append(heading)
                result.extend(part+'×1' for part in implicit)
                continue
            if tail:
                result.extend([heading,tail])
            else:result.append(heading)
    if subtotal is not None:result.append('小计'+subtotal+'支')
    # Percentage is part of a catalog identity, not an item count. Accept
    # reversed notation only when the same base and percentage identify one
    # live flavor in the explicit category; never create a permanent Alias.
    current=None
    for index,line in enumerate(result):
        heading=re.fullmatch(r'(?:品类[:：])?(.+)',line.strip())
        if heading and heading.group(1) in targets:
            current=heading.group(1)
            continue
        for category in targets:
            if category and _heading_match(category,line.lstrip()):
                current=category
                break
        if not current:continue
        # Percent-bearing live identities and their approved aliases are
        # complete flavor tokens, not a prefix quantity plus another item.
        pool=[p for p in products if p.get('parent_name')==current and p.get('is_active',True)
              and not p.get('is_archived')]
        # Numeric flavor names such as 100号 are live identities, not room
        # numbers. Shield only an exact full catalog token under a proven
        # category; generic numeric/address-like text remains untouched.
        numeric=[]
        for product in pool:
            label=str(product.get('name') or '')
            if not re.match(r'^\d',label):continue
            # Formatting whitespace inside an already proven numeric catalog
            # identity (100 号) must not turn it into an address or quantity.
            identity=r'\s*'.join(re.escape(part) for part in re.findall(r'\d+|[^\d\s]+',label))
            found=re.fullmatch(identity+r'\s*(?:(?:[*×xX]\s*)?(\d+)\s*(?:支|根|个|件)?)?',line.strip(),re.I)
            if found and int(found.group(1) or 1)>0:numeric.append((label,int(found.group(1) or 1)))
        if len(set(numeric))==1:
            flavor,quantity=numeric[0]
            result[index]=f'品类({current})口味({flavor})×{quantity}'
            continue
        labels={str(p.get('name')):{str(p.get('name'))} for p in pool if '%' in str(p.get('name'))}
        for row in rows:
            if (row.get('type')=='EXPLICIT_FLAVOR' and row.get('status')=='active'
                    and row.get('category')==current and '%' in str(row.get('alias'))
                    and any(scope_matches(row,p) for p in pool)):
                target=row['standard'] if any(p.get('name')==row.get('standard') for p in pool) else row['alias']
                labels.setdefault(row['alias'],set()).add(target)
        percent_items=[]
        for label,standards in labels.items():
            match=re.fullmatch(re.escape(label)+r'\s*(?:[*×xX]\s*)?(\d+)\s*(?:支|根|个|件)?',line.strip(),re.I)
            if match and len(standards)==1 and int(match.group(1))>0:
                percent_items.append((next(iter(standards)),int(match.group(1))))
        if len(set(percent_items))==1:
            flavor,quantity=percent_items[0]
            result[index]=f'品类({current})口味({flavor})×{quantity}'
            continue
        variants={}
        for product in products:
            if product.get('parent_name')!=current or not product.get('is_active',True):continue
            name=str(product.get('name') or '')
            percentage=re.fullmatch(r'(\d+(?:\.\d+)?)%(.+)',name)
            if percentage:
                alternate=percentage.group(2)+percentage.group(1)+'%'
                variants.setdefault(alternate,set()).add(name)
        for alternate,names in sorted(variants.items(),key=lambda pair:-len(pair[0])):
            if len(names)!=1:continue
            # A complete item token, not part of an address or another flavor.
            line=re.sub(r'(?<![\w%])'+re.escape(alternate)+r'(?=\s*(?:[*×xX]\s*\d|\d+\s*(?:支|根|个|件)?(?:[,，、;；\s]|$)))',
                        lambda match:next(iter(names)),line)
        result[index]=line
    # Keep explicitly labelled category scope after heading normalisation.
    # The shared rules cannot infer every live catalogue category from a
    # bare name; losing it can assign a common flavor to a different brand.
    return '\n'.join('品类：'+line if line in explicit_subtotal_categories else line for line in result)
