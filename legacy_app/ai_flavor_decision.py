"""Catalog-bound per-order AI suggestions, never permanent alias approval."""
import math
from deepseek_resolver import resolve_category, resolve_identity_flavor as resolve_flavor

def evidence_in(source, text):
    """Formatting-insensitive evidence, not semantic synonym authorization."""
    import re, unicodedata
    def norm(value):
        return re.sub(r'[\s:：,，、()（）]', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()
    return bool(norm(source)) and norm(source) in norm(text)

def selected_mutual_pairs(raw_text, items, products):
    """The confirmed colloquial instruction authorizes exactly two selections.

    It is never arbitrary random, and cannot override another backup directive.
    Catalog/source validation remains mandatory for both selected flavors.
    """
    import re
    from order_structure import structure_order_text, catalog_names, fulfillment_parts
    from contact_fields import merchandise_text
    if not re.search(r'(?:备选|备用)\s*发\s*自己要的',str(raw_text or '')):return []
    notes=structure_order_text(raw_text,products=products).fulfillment_notes
    if len(notes)!=1:return []
    parts=fulfillment_parts(notes[0],catalog_names(products))
    if not parts or parts[0].strip() or parts[2]:return []
    if not re.fullmatch(r'自己要的(?:哈)?\s*(?:谢谢(?:哒)?)?[。.!！]?\s*',parts[1]):return []
    mains=[i for i in items if isinstance(i,dict)]
    if len(mains)!=2 or len({i.get('flavor') for i in mains})!=2:return []
    # Reuse existing category/alias/source checks with the merchandise-only
    # evidence prefix; authorization came from the exact customer note above.
    goods=structure_order_text(merchandise_text(raw_text,products=products),products=products).text
    goods='\n'.join(line for line in goods.splitlines()
                    if not re.search(r'地址|电话|手机|收件|收货',line))
    return mutual_backup_pairs(goods+'（互为备选）',mains,products)

def mutual_backup_pairs(note, items, products):
    """Narrow source-bound mutual authorization, never purchase quantities."""
    import re
    from order_structure import fulfillment_parts, catalog_names
    from rule_dictionary import read_store
    from brand_aliases import scope_matches
    parts=fulfillment_parts(note,catalog_names(products))
    if not parts or parts[1]!='互为备选':return []
    if re.search(r'地址|收件|电话|手机|(?:不|非|禁止|不可|不要)\s*(?:互为备选|互相备选(?:口味)?)',note):return []
    mains=[i for i in items if isinstance(i,dict)]
    categories={i.get('category') for i in mains}
    if len(categories)!=1 or None in categories or len(mains)<2:return []
    category=next(iter(categories))
    pool=[p for p in products if p.get('parent_name')==category and p.get('is_active',True) and not p.get('is_archived')]
    names=list(dict.fromkeys(p['name'] for p in pool))
    selected=[]
    for item in mains:
        flavor=item.get('flavor');raw=item.get('raw_flavor') or flavor
        if flavor not in names:return []
        candidates=[raw,flavor]
        candidates.extend(r.get('alias') for r in read_store().get('aliases',[]) if r.get('status')=='active'
            and r.get('type')=='EXPLICIT_FLAVOR' and r.get('category')==category and r.get('standard')==flavor
            and any(scope_matches(r,p) for p in pool if p['name']==flavor))
        evidenced=False
        for token in candidates:
            if not token or not evidence_in(token,parts[0]):continue
            canonical,_=resolve_flavor(category,token,names,products=pool)
            if canonical==flavor:evidenced=True;break
        if not evidenced:return []
        if flavor not in selected:selected.append(flavor)
    if len(selected)<2:return []
    return [(a,b) for a in selected for b in selected if a!=b]

def proposals(result, raw_text, products, confirmed=()):
    if not isinstance(result,dict):return []
    live=[p for p in products if p.get('name') and p.get('is_active',True) and not p.get('is_archived')]
    categories=list(dict.fromkeys(p.get('parent_name') for p in live if p.get('parent_name')))
    pending=[]
    for index,item in enumerate(result.get('items') or []):
        if index in confirmed or not isinstance(item,dict):continue
        raw_category=str(item.get('raw_category') or item.get('category') or '').strip()
        raw_flavor=str(item.get('raw_flavor') or item.get('flavor') or '').strip()
        source=str(item.get('source_text') or '').strip()
        if not evidence_in(raw_flavor,raw_text) or not evidence_in(raw_category,raw_text):
            continue
        if source and not evidence_in(source,raw_text):continue
        category,_=resolve_category(raw_category,categories,live)
        if not category:continue
        pool=[p for p in live if p.get('parent_name')==category]
        names=list(dict.fromkeys(p['name'] for p in pool))
        canonical,_=resolve_flavor(category,raw_flavor,names,products=pool)
        if canonical:continue  # Standards and reviewed aliases precede AI.
        # Reliable, source-bound local evidence precedes advisory provider lists.
        from typo_confirmation import candidate_names
        candidates=candidate_names(raw_flavor,names)
        choices=list(dict.fromkeys(c for c in candidates if c in names))
        from flavor_aliases import mapping_rejected
        from rule_dictionary import runtime_store
        store=runtime_store()
        choices=[name for name in choices if any(not mapping_rejected(store,dict(type='EXPLICIT_FLAVOR',
            alias=raw_flavor,category=category,standard=name,category_id=p.get('parent_id'),
            product_id=p['id'],warehouse_id=p.get('default_warehouse_id'))) for p in pool if p['name']==name)]
        if not choices:continue
        try:confidence=float(item.get('confidence',result.get('confidence',0)))
        except (ValueError,TypeError):confidence=0
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:confidence=0
        # Even one high-confidence model suggestion is not identity evidence.
        # Keep it in the existing Pending flow until the customer selects it.
        pending.append(dict(index=index,raw_flavor=raw_flavor,category=category,choices=choices,
            product_ids={name:[p['id'] for p in pool if p['name']==name] for name in choices}))
    from order_structure import structure_order_text, fulfillment_parts, catalog_names
    protected=catalog_names(products)
    notes=structure_order_text(raw_text,names=protected).fulfillment_notes
    selected_pairs=set(selected_mutual_pairs(raw_text,result.get('items') or [],products))
    mutual_pairs={p for note in notes for p in mutual_backup_pairs(note,result.get('items') or [],products)}|selected_pairs
    for alternate_index, alternate in enumerate(result.get('alternates') or []):
        index=len(result.get('items') or [])+alternate_index
        if index in confirmed or not isinstance(alternate,dict):continue
        raw_flavor=str(alternate.get('raw_flavor') or alternate.get('flavor') or '').strip()
        # Backup evidence must be in an explicit authorized backup tail, not
        # merely anywhere in the customer's Contact or product text.
        mutual_authorized=((alternate.get('for'),alternate.get('flavor')) in selected_pairs and
            raw_flavor==alternate.get('flavor')) or ((alternate.get('for'),alternate.get('flavor')) in mutual_pairs and
            any(evidence_in(raw_flavor,fulfillment_parts(note,protected)[0]) for note in notes
                if fulfillment_parts(note,protected) and fulfillment_parts(note,protected)[1]=='互为备选'))
        if not raw_flavor or not (mutual_authorized or any(evidence_in(raw_flavor,m[1]) for note in notes
                for m in [fulfillment_parts(note,protected)] if m)):continue
        mains=[item for item in result.get('items') or [] if isinstance(item,dict)
               and alternate.get('for') in (item.get('flavor'),item.get('raw_flavor'))]
        if len(mains)!=1:continue
        main=mains[0]
        virtual=dict(category=main.get('category'),raw_category=main.get('raw_category'),
            flavor=alternate.get('flavor'),raw_flavor=raw_flavor,
            source_text=alternate.get('source_text') or raw_flavor,
            confidence=alternate.get('confidence',result.get('confidence',0)),
            flavor_candidates=alternate.get('flavor_candidates') or [])
        virtual_result=dict(items=[virtual],confidence=result.get('confidence',0),
                            needs_manual_review=result.get('needs_manual_review',False))
        for proposal in proposals(virtual_result,raw_text,products):
            pending.append(dict(proposal,index=index,kind='alternate',alternate_index=alternate_index))
    return pending

def authorize(result, proposal, choice):
    if choice not in proposal['choices']:raise ValueError('候选选择无效')
    import copy
    accepted=copy.deepcopy(result)
    if proposal.get('kind')=='alternate':
        item=accepted['alternates'][proposal['alternate_index']]
        item.update(flavor=choice)
    else:
        item=accepted['items'][proposal['index']]
        previous=(item.get('raw_flavor'),item.get('flavor'))
        if sum(i.get('raw_flavor')==item.get('raw_flavor') for i in accepted['items'])==1:
            for alternative in accepted.get('alternates') or []:
                if alternative.get('for') in previous:alternative['for']=choice
        item.update(category=proposal['category'],flavor=choice)
    # The caller owns the confirmation set; provider JSON cannot claim consent.
    return accepted


def prepare_unique_candidates(result, raw_text, products):
    """Model confidence never substitutes for customer button consent."""
    return result


def fulfillment_evidence_errors(result, raw_text, products):
    """AI may interpret customer backup instructions, never invent consent."""
    import re
    from order_structure import structure_order_text, fulfillment_parts, catalog_names, syntax_text
    protected=catalog_names(products)
    notes=structure_order_text(raw_text,names=protected).fulfillment_notes
    policy=(result.get('fulfillment') or {}).get('on_out_of_stock')
    errors=[]
    selected_pairs=set(selected_mutual_pairs(raw_text,result.get('items') or [],products))
    mutual_pairs={p for note in notes for p in mutual_backup_pairs(note,result.get('items') or [],products)}|selected_pairs
    if policy=='repeat_selected' and not any(re.search(r'(?:缺货|没货)(?:就)?\s*(?:发|用)?\s*重复',syntax_text(note,protected)) for note in notes):
        errors.append('缺货重复缺少客户原文授权')
    if policy=='random' and not any(re.search(r'(?:缺货|没货|备选|备用)\s*[:：]?\s*随机',syntax_text(note,protected)) for note in notes):
        errors.append('缺货随机缺少客户原文授权')
    for alternate in result.get('alternates') or []:
        raw=str(alternate.get('raw_flavor') or alternate.get('flavor') or '').strip()
        mutual_authorized=((alternate.get('for'),alternate.get('flavor')) in selected_pairs and
            raw==alternate.get('flavor')) or ((alternate.get('for'),alternate.get('flavor')) in mutual_pairs and
            any(evidence_in(raw,fulfillment_parts(note,protected)[0]) for note in notes
                if fulfillment_parts(note,protected) and fulfillment_parts(note,protected)[1]=='互为备选'))
        if not raw or not (mutual_authorized or any(evidence_in(raw,parts[1]) for note in notes
                for parts in [fulfillment_parts(note,protected)] if parts)):
            errors.append('缺货备选缺少客户原文授权');continue
        source=str(alternate.get('source_text') or '').strip()
        if source and not evidence_in(source,raw_text):
            errors.append('缺货备选证据不属于客户原文')
    return errors


def normalize_repeat_selected(result, raw_text, products):
    """Source-authorized repeat is not arbitrary random or a named flavor."""
    import re
    from order_structure import structure_order_text, catalog_names, syntax_text
    protected=catalog_names(products)
    notes=structure_order_text(raw_text,names=protected).fulfillment_notes
    mutual_result=result
    if re.search(r'(?:备选|备用)\s*发\s*自己要的',str(raw_text or '')) and len(result.get('items') or [])==2:
        # Use the same deterministic resolver as the ordinary Validator.
        # Historical/provider display names must not preempt current audited
        # raw-flavor Alias resolution. No quantities or evidence are invented.
        from deepseek_resolver import resolve_item
        normalized=[]
        live=[p for p in products if p.get('is_active',True) and not p.get('is_archived')]
        for item in result['items']:
            resolved=resolve_item(item,live)
            if not resolved.get('resolved_category') or not resolved.get('resolved_flavor'):break
            normalized.append(dict(item,category=resolved['resolved_category'],flavor=resolved['resolved_flavor']))
        if len(normalized)==2:mutual_result=dict(result,items=normalized)
    pairs=selected_mutual_pairs(raw_text,mutual_result.get('items') or [],products)
    if pairs:
        return dict(mutual_result,fulfillment=dict(result.get('fulfillment') or {},on_out_of_stock='alternate',selection_scope='selected_mutual'),
                    alternates=[dict(**{'for':a},flavor=b,raw_flavor=b) for a,b in pairs])
    if not any(re.search(r'(?:缺货|没货)(?:就)?\s*(?:发|用)?\s*重复',syntax_text(note,protected)) for note in notes):
        return result
    return dict(result,fulfillment=dict(result.get('fulfillment') or {},on_out_of_stock='repeat_selected'),alternates=[])
