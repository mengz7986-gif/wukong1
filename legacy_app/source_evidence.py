"""Locate a real source span after lossless numeric-unit normalization only."""
import re
import unicodedata

def bind_reviewed_sources(result, original, products):
    """Recover omitted raw tokens only through effective, catalog-bound Alias.

    Never infer a synonym or waive source evidence. Both canonical identity
    and the real customer token remain in the same item snapshot.
    """
    if not isinstance(result,dict):return result
    import copy
    from rule_dictionary import read_store
    from brand_aliases import scope_matches
    from contact_fields import merchandise_text
    accepted=copy.deepcopy(result)
    evidence=merchandise_text(original, products=products)
    rows=read_store().get('aliases',[])
    for item in accepted.get('items') or []:
        if not isinstance(item,dict):continue
        for raw_key,target_key,kind in [('raw_category','category','MODEL_ALIASES'),('raw_flavor','flavor','EXPLICIT_FLAVOR')]:
            target=str(item.get(target_key) or '')
            raw=str(item.get(raw_key) or target)
            if raw in evidence or raw!=target:continue
            pool=[p for p in products if p.get('is_active',True) and not p.get('is_archived')
                  and (p.get('parent_name')==target if kind=='MODEL_ALIASES' else
                       p.get('parent_name')==item.get('category') and p.get('name')==target)]
            candidates=[]
            for row in rows:
                alias=str(row.get('alias') or '')
                if (row.get('status')=='active' and row.get('type')==kind and row.get('standard')==target
                        and alias and alias in evidence and any(scope_matches(row,p) for p in pool)
                        and (kind=='MODEL_ALIASES' or row.get('category')==item.get('category'))):
                    candidates.append(alias)
            if candidates:
                item[raw_key]=max(candidates,key=len)
                item['match_type']='alias'
    # Providers sometimes repeat the authorization prefix for each backup
    # ("备选 桃子", "备选 可乐") although the customer wrote it only once.
    # Recover the token exclusively from the explicit backup tail, then let
    # the existing catalog/AI-candidate Validator decide its identity.
    from order_structure import structure_order_text, fulfillment_parts, catalog_names
    protected=catalog_names(products,rows)
    tails=[part[1] for note in structure_order_text(original,names=protected).fulfillment_notes
           for part in [fulfillment_parts(note,protected)] if part]
    for item in accepted.get('alternates') or []:
        if not isinstance(item,dict):continue
        raw=str(item.get('raw_flavor') or '')
        if not raw:
            proposed=str(item.get('source_text') or '')
            part=fulfillment_parts(proposed,protected)
            candidate=part[1] if part else ''
            if candidate and not re.search(r'[,，、;；\n]|或者|或',candidate) and any(candidate in tail for tail in tails):
                raw=candidate
                # Recovering a raw token is not proof of its semantics. Force
                # the existing catalog/candidate refinement to revalidate it.
                item.update(flavor=raw,flavor_candidates=[],confidence=0)
        if raw and any(raw in tail for tail in tails):
            item['raw_flavor']=raw
            item['source_text']=raw
    return accepted

def indexed(text):
    from order_structure import _number
    out=[]; positions=[]
    cursor=0
    for match in re.finditer(r'([零一二两三四五六七八九十]{1,3})(根|支|件|个|条)', text):
        for index in range(cursor, match.start()):
            for ch in unicodedata.normalize('NFKC', text[index]).casefold():
                if not ch.isspace():out.append(ch);positions.append((index,index+1))
        for ch in _number(match):
            out.append(ch);positions.append((match.start(),match.end()))
        cursor=match.end()
    for index in range(cursor,len(text)):
        for ch in unicodedata.normalize('NFKC', text[index]).casefold():
            if not ch.isspace():out.append(ch);positions.append((index,index+1))
    return ''.join(out),positions

def actual_span(original, proposed):
    if not proposed:return None
    if proposed in original:return proposed
    haystack,positions=indexed(original);needle,_=indexed(proposed)
    if not needle:return None
    matches=[m.start() for m in re.finditer(re.escape(needle),haystack)]
    if len(matches)!=1:return None
    start=matches[0];end=start+len(needle)-1
    return original[positions[start][0]:positions[end][1]]

def restore(result, original):
    if not isinstance(result,dict):return result
    import copy
    restored=copy.deepcopy(result)
    for item in [*(restored.get('items') or []),*(restored.get('alternates') or [])]:
        if not isinstance(item,dict):continue
        source=actual_span(original,str(item.get('source_text') or ''))
        if source is not None:item['source_text']=source
    return restored

def quantity_conflict(original,item,products=()):
    """A provider may not override an explicit flavor-line count."""
    from order_structure import structure_order_text
    from contact_fields import merchandise_text
    # Whitespace equivalence is useful within an item span, but joining
    # lines binds an address number or section subtotal to the next flavor.
    # Keep lexical separators: collapsing "2龙井 1青提" makes the next
    # item's prefix look like the current flavor's suffix quantity.
    lines = [' '.join(indexed(part)[0] for part in re.split(r'\s+',line)) for line in
             structure_order_text(merchandise_text(original,products=products),products).text.splitlines()]
    flavor,_=indexed(str(item.get('raw_flavor') or item.get('flavor') or ''))
    if not flavor:return False
    values=[]
    from learning_knowledge import _chinese_quantity
    raw_flavor=str(item.get('raw_flavor') or item.get('flavor') or '')
    if raw_flavor:
        for match in re.finditer(re.escape(raw_flavor)+r'[(（]([^()（）]{1,8})(?:根|支|件|个|条)[)）]',original):
            number=_chinese_quantity(match.group(1))
            if number is not None:values.append(number)
    for line in lines:
        for pattern in (re.escape(flavor)+r'[)）]*(?:[x×*:：])?(\d+)(?!\d)(?=$|[\s,，、;；()（）]|支|根|个|件)',
                        r'(?:^|[\s,，、;；:：])(?<!\d)(\d+)(?:支|根|个|件)?'+re.escape(flavor)):
            values.extend(int(m.group(1)) for m in re.finditer(pattern,line))
    # A normalized flavor may differ from the customer's spelling. Retain
    # the count on a uniquely evidenced item span, rather than falling back
    # to one because the canonical name is absent from the source.
    span=actual_span(original,str(item.get('source_text') or ''))
    if span:
        from order_structure import catalog_names,syntax_text
        source=syntax_text(unicodedata.normalize('NFKC',span),catalog_names(products)).strip()
        source=re.sub(r'(\d+\s*(?:根|支|件|个)?)[(][^()\d]*[)]\s*$',r'\1',source)
        explicit=re.fullmatch(r'[^\d]+?(?:[xX×*]\s*|\s+)?(\d+)\s*(?:根|支|件|个)?',source)
        # An actual, uniquely located "2龙井" span is direct evidence for
        # this identity, independent of provider item-array ordering.
        prefix=re.fullmatch(r'(\d+)\s*(?:根|支|件|个)?\s*([^\d]+)',source)
        if prefix and indexed(prefix.group(2))[0] == flavor:
            values.append(int(prefix.group(1)))
        if (explicit and not re.search(r'1[3-9]\d{9}|收件|姓名|手机|电话|地址|省|市|区|县|路|街|楼|栋|室',source)
                and not re.fullmatch(r'(?:共|共计|合计|总计|总共|总数|数量)\s*\d+\s*(?:根|支|件|个)?',source)):
            values.append(int(explicit.group(1)))
    try:quantity=int(item.get('quantity'))
    except (ValueError,TypeError):return True
    return bool(values) and quantity not in values
