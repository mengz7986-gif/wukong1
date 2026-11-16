"""One bounded, brand-scoped candidate request when extraction supplies none."""
import copy,json,math
from deepseek_resolver import resolve_category,resolve_flavor
from ai_flavor_decision import evidence_in

def candidate_request(payload):
    from ai_settings import get_settings
    from ai_transport import completion
    settings=get_settings()
    if not settings.get('enabled') or not settings.get('order_enabled') or not settings.get('api_key'):
        return {}
    return completion(settings,[
        {'role':'system','content':'仅判断客户口味叫法与给定品类目录的语义关系。返回JSON candidates（0至3个目录中逐字存在的口味名）、confidence（映射置信度0至1）。不得跨品类、创造商品、猜数量。没有可靠近义关系返回空数组；不确定可列多个候选供客户选择。'},
        {'role':'user','content':json.dumps(payload,ensure_ascii=False)}
    ],'ORDER_FLAVOR_CANDIDATE',validator=json.loads)


async def refine_async(result,raw_text,products):
    """Collect unresolved scoped flavors locally, then make at most one call."""
    import asyncio
    from ai_transport import run_provider_worker
    requests={}
    def key(payload):return json.dumps(payload,sort_keys=True,ensure_ascii=False)
    def collect(payload):
        identity=key(payload)
        if identity not in requests and len(requests)<4:requests[identity]=payload
        return {}
    local=await asyncio.to_thread(refine,result,raw_text,products,collect)
    if not requests:return local
    entries=list(requests.items())
    try:
        if len(entries)==1:
            answer=await run_provider_worker(candidate_request,entries[0][1])
            answers={entries[0][0]:answer}
        else:
            response=await run_provider_worker(candidate_batch_request,[p for _,p in entries])
            values=response.get('results') if isinstance(response,dict) else None
            if not isinstance(values,list):return local
            # Duplicate IDs are ambiguous and must not overwrite an earlier
            # answer; an omitted result remains unresolved, never guessed.
            answers={}
            for index,(identity,_) in enumerate(entries):
                matches=[r for r in values if isinstance(r,dict) and type(r.get('id')) is int and r['id']==index]
                if len(matches)==1:answers[identity]=matches[0]
    except Exception:return local
    return await asyncio.to_thread(refine,result,raw_text,products,lambda p:answers.get(key(p),{}))

def candidate_batch_request(payloads):
    from ai_settings import get_settings
    from ai_transport import completion
    settings=get_settings()
    if not settings.get('enabled') or not settings.get('order_enabled') or not settings.get('api_key'):return {}
    entries=[dict(p,id=i) for i,p in enumerate(payloads[:4])]
    return completion(settings,[
        {'role':'system','content':'按id分别判断客户口味与该项品类目录的语义关系。返回JSON results数组，每项id、candidates（0至3个该项目录中的逐字口味名）、confidence（0至1）。禁止跨项目跨品类映射，禁止创造商品、数量。证据不足返回空数组。'},
        {'role':'user','content':json.dumps(entries,ensure_ascii=False)}
    ],'ORDER_FLAVOR_CANDIDATE',validator=json.loads)

def refine(result,raw_text,products,request=None):
    if not isinstance(result,dict):return result
    from source_evidence import bind_reviewed_sources
    accepted=bind_reviewed_sources(result,raw_text,products)
    # Some providers supply a standard backup but omit its original alias.
    # Recover only one unambiguous authorized raw span, then re-run normal
    # catalog/AI resolution; never inherit invented confidence or consent.
    import re
    from order_structure import structure_order_text,fulfillment_parts,catalog_names
    protected=catalog_names(products)
    notes=structure_order_text(raw_text,names=protected).fulfillment_notes
    for alternate in accepted.get('alternates') or []:
        if not isinstance(alternate,dict) or alternate.get('raw_flavor'):continue
        mains=[item for item in accepted.get('items') or [] if isinstance(item,dict)
               and alternate.get('for') in (item.get('flavor'),item.get('raw_flavor'))]
        source=str(alternate.get('source_text') or '')
        if len(mains)!=1 or not source or not evidence_in(source,raw_text):continue
        spans=[]
        for note in notes:
            parts=fulfillment_parts(note,protected)
            if not parts or not evidence_in(source,note):continue
            options=[s.strip() for s in re.split(r'[,，、/／]|或者|或',parts[1]) if s.strip()]
            if len(options)==1:spans.extend(options)
            else:
                category=mains[0].get('category')
                pool=[p for p in products if p.get('is_active',True) and not p.get('is_archived') and p.get('parent_name')==category]
                names=list(dict.fromkeys(p['name'] for p in pool))
                target=alternate.get('flavor')
                # Restore a raw backup only when an already-effective catalog
                # name/Alias proves its identity. Multiple matches stay unresolved.
                for option in options:
                    canonical,_=resolve_flavor(category,option,names,products=pool)
                    if canonical and canonical==target:spans.append(option)
        spans=list(dict.fromkeys(spans))
        if len(spans)==1:
            alternate.update(raw_flavor=spans[0],flavor=spans[0],flavor_candidates=[],confidence=0)
    # If one raw entry remains in a fully evidenced, same-brand backup list,
    # its identity can be recovered by elimination without guessing a standard.
    # It still must pass catalog/AI candidate resolution and customer consent.
    for alternate in accepted.get('alternates') or []:
        if not isinstance(alternate,dict) or alternate.get('raw_flavor'):continue
        source=str(alternate.get('source_text') or '')
        siblings=[a for a in accepted.get('alternates') or [] if isinstance(a,dict) and a.get('source_text')==source]
        mains=[i for i in accepted.get('items') or [] if isinstance(i,dict) and alternate.get('for') in (i.get('flavor'),i.get('raw_flavor'))]
        if len(mains)!=1 or not source or not evidence_in(source,raw_text):continue
        if len({i.get('category') for i in accepted.get('items') or [] if isinstance(i,dict)})!=1:continue
        unknown=[a for a in siblings if not a.get('raw_flavor')]
        if len(unknown)!=1:continue
        for note in notes:
            parts=fulfillment_parts(note,protected)
            if not parts or not evidence_in(source,note):continue
            options=list(dict.fromkeys(s.strip() for s in re.split(r'[,，、/／]|或者|或',parts[1]) if s.strip()))
            known={a.get('raw_flavor') for a in siblings if a.get('raw_flavor')}
            remaining=[s for s in options if s not in known]
            if len(options)==len(siblings) and len(remaining)==1 and known.issubset(set(options)):
                alternate.update(raw_flavor=remaining[0],flavor=remaining[0],flavor_candidates=[],confidence=0)
    live=[p for p in products if p.get('is_active',True) and p.get('parent_name') and p.get('name')]
    categories=list(dict.fromkeys(p['parent_name'] for p in live))
    attempts=0
    for item in accepted.get('items') or []:
        if not isinstance(item,dict) or attempts>=4:continue
        raw_category=str(item.get('raw_category') or item.get('category') or '')
        raw_flavor=str(item.get('raw_flavor') or item.get('flavor') or '')
        if not raw_category or raw_category not in raw_text or not raw_flavor or raw_flavor not in raw_text:continue
        if item.get('source_text') and not evidence_in(item['source_text'],raw_text):continue
        category,_=resolve_category(raw_category,categories)
        if not category:continue
        pool=[p for p in live if p['parent_name']==category]
        names=list(dict.fromkeys(p['name'] for p in pool))
        standard,_=resolve_flavor(category,raw_flavor,names,products=pool)
        if standard:
            # Current standards/reviewed aliases outrank a provider's invented
            # normalized spelling. Keep raw source/count/confidence unchanged.
            item.update(category=category,flavor=standard,flavor_candidates=[standard])
            continue
        if any(c in names for c in item.get('flavor_candidates') or []) or item.get('flavor') in names:continue
        attempts+=1
        if request is None:
            request=candidate_request
        try:
            value=request({'category':category,'customer_flavor':raw_flavor,'catalog':names[:200]})
            if not isinstance(value,dict) or not isinstance(value.get('candidates'),list):continue
            candidates=list(dict.fromkeys(c for c in value['candidates'][:3] if isinstance(c,str) and c in names))
            confidence=float(value.get('confidence',0))
            if not math.isfinite(confidence) or not 0<=confidence<=1:continue
            if candidates:item.update(flavor_candidates=candidates,confidence=confidence)
        except Exception:
            # Candidate discovery is advisory. No provider failure can accept
            # an unknown product or repeat any order/inventory action.
            continue
    # A backup's category comes from its uniquely linked main item, never
    # from a global flavor search. No explicit customer authorization, no AI.
    from order_structure import structure_order_text,fulfillment_parts
    notes=structure_order_text(raw_text,names=protected).fulfillment_notes
    for alternate in (accepted.get('alternates') or [])[:max(0,4-attempts)]:
        if not isinstance(alternate,dict):continue
        raw=str(alternate.get('raw_flavor') or alternate.get('flavor') or '')
        if not raw or not any(evidence_in(raw,m[1]) for note in notes for m in [fulfillment_parts(note,protected)] if m):continue
        mains=[item for item in accepted.get('items') or [] if isinstance(item,dict) and alternate.get('for') in (item.get('raw_flavor'),item.get('flavor'))]
        if len(mains)!=1:continue
        virtual=dict(mains[0],raw_flavor=raw,flavor=alternate.get('flavor') or raw,
                     source_text=alternate.get('source_text') or raw,
                     flavor_candidates=alternate.get('flavor_candidates') or [])
        enriched=refine({'items':[virtual]},raw_text,products,request)['items'][0]
        if enriched.get('flavor_candidates'):
            alternate.update(flavor=enriched.get('flavor',alternate.get('flavor')),
                             flavor_candidates=enriched['flavor_candidates'],confidence=enriched.get('confidence',0))
    return accepted
