"""Unique catalog-bound spelling/abbreviation suggestions, per-order consent."""
import re
from collections import Counter

def category_candidates(raw,products):
    """Visual spelling candidates only, never an effective category Alias."""
    return []  # Classification is never a suggestion.

def candidate_names(flavor,names):
    if not re.fullmatch(r'[\u4e00-\u9fff]{2,20}',flavor):return []
    candidates=[]
    for name in names:
        if not re.fullmatch(r'[\u4e00-\u9fff]{2,23}',name):continue
        typo=len(name)==len(flavor)==4 and sum(a!=b for a,b in zip(name,flavor))==1
        # Explicit ordered abbreviation: at most three omitted characters,
        # at least 70% of the real name retained. Never cross category/spec.
        chars=iter(name)
        abbreviation=(len(flavor)>=4 and len(flavor)<len(name)<=len(flavor)+3 and len(flavor)*10>=len(name)*7
                      and all(any(c==part for c in chars) for part in flavor))
        ice_reordered=(len(flavor)==len(name) and ((flavor.startswith('冰') and name==flavor[1:]+'冰')
                        or (name.startswith('冰') and flavor==name[1:]+'冰')))
        icecream_variant=flavor.replace('冰激凌','冰淇淋').replace('冰淇凌','冰淇淋')==name and flavor!=name
        reordered=flavor!=name and len(flavor)>=4 and Counter(flavor)==Counter(name)
        short_literal=len(flavor)>=2 and flavor!=name and flavor in name
        trailing_noise=(len(name)>=4 and flavor.endswith('色') and len(flavor)==len(name)+1
                        and flavor[:2]==name[:2] and sum(a!=b for a,b in zip(flavor[:-1],name))<=1)
        composite=len(name)>=2 and name in flavor and name!=flavor
        if composite or typo or abbreviation or ice_reordered or icecream_variant or reordered or short_literal or trailing_noise:candidates.append(name)
    return candidates


def local_result(order, original, products):
    if not isinstance(order,dict) or not order.get('items') or order.get('quantity_scope_errors'):
        return None
    from deepseek_resolver import resolve_identity_flavor, resolve_category
    from ai_flavor_decision import proposals,evidence_in
    items=[];has_typo=False
    for reference, quantity in order['items']:
        match=re.fullmatch(r'品类\(([^()]*)\)口味\(([^()]*)\)',str(reference))
        if not match or type(quantity) is not int or quantity<=0:return None
        category,flavor=match.groups()
        if not evidence_in(flavor,original) or not evidence_in(category,original):return None
        raw_category=category
        category,_=resolve_category(category,list({p.get('parent_name') for p in products if p.get('parent_name')}),products)
        if not category:return None
        pool=[p for p in products if p.get('parent_name')==category and p.get('is_active',True) and not p.get('is_archived')]
        names=list(dict.fromkeys(p['name'] for p in pool))
        resolved,_=resolve_identity_flavor(category,flavor,names,products=pool)
        if resolved:
            target=resolved;candidates=[]
        else:
            candidates=candidate_names(flavor,names)
            if not candidates:return None
            target=candidates[0] if candidates else flavor
            if candidates:has_typo=True
        item=dict(category=category,raw_category=raw_category,flavor=target,raw_flavor=flavor,
                  source_text=flavor,quantity=quantity,confidence=1.0)
        if candidates:item['flavor_candidates']=candidates
        items.append(item)
    # Reuse source-scoped fulfillment spans; backups never become primary units.
    from local_fulfillment import scoped_notes
    from order_structure import fulfillment_parts,catalog_names
    alternates=[]
    notes=scoped_notes(original,items,products) if re.search(r'备选|备用|替补|缺货|没货',str(original)) else ()
    for note,category in notes:
        parts=fulfillment_parts(note,catalog_names(products))
        prefix,body,_=parts
        if body.strip() in ('无','没有','无备选','暂无'):continue
        if re.search(r'随机|精准|互为|重复|自己|退单',body):continue
        mains=[i for i in items if i['category']==category]
        if prefix.strip():mains=[i for i in mains if evidence_in(i['raw_flavor'],prefix) or evidence_in(i['flavor'],prefix)]
        if len(mains)!=1:return None
        main=mains[0];pool=[p for p in products if p.get('parent_name')==category and p.get('is_active',True) and not p.get('is_archived')]
        names=list(dict.fromkeys(p['name'] for p in pool))
        for raw in re.split(r'[,，、/／]|或者|或',body):
            raw=raw.strip()
            if not raw:continue
            canonical,_=resolve_identity_flavor(category,raw,names,products=pool)
            candidates=[] if canonical else candidate_names(raw,names)
            if not canonical and not candidates:return None
            if candidates:has_typo=True
            alternates.append(dict(**{'for':main['raw_flavor']},flavor=canonical or raw,raw_flavor=raw,
                                   source_text=raw,flavor_candidates=candidates,confidence=1.0))
    if not has_typo:return None
    result=dict(items=items,recipient=order.get('recipient'),phone=order.get('phone'),address=order.get('address'),
                confidence=1.0,missing=[],needs_manual_review=False,alternates=alternates,notes=[],
                fulfillment={'on_out_of_stock':'alternate'} if alternates else {})
    choices=proposals(result,original,products)
    return (result,choices) if choices else None
