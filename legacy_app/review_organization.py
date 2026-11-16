"""Sort recognition observations into existing panels, never approve or replay."""
import hashlib
from datetime import datetime,timezone
import failure_analysis as reviews
from brand_aliases import transaction


def organize(catalog, principals=None, limit=None):
    principals=principals or {}
    with reviews.database() as connection:
        from datetime import timedelta
        cutoff=(datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat()
        records=[dict(row) for row in connection.execute("SELECT * FROM recognition_reviews WHERE status IN ('pending','observed') AND (? IS NULL OR updated < ?) ORDER BY updated LIMIT ?",(limit,cutoff,limit or -1))]
    if not records:return {'records':0}
    proposals=[];grammar=[]
    from learning_pipeline import forms_from_validated_order
    for row in records:
        payload=reviews.unpack(row['ciphertext'])
        raw=payload.get('original') or ''
        suggestions=list(payload.get('suggestions') or [])
        # Similarity is only a pending suggestion, never authorization.
        suggestions += [r for r in payload.get('local_candidates') or [] if r.get('similarity',0)>=.5]
        for suggestion in suggestions:
            # Failure/AI observations are analysis, not customer consent.
            if suggestion.get('kind')=='FLAVOR_ALIAS':continue
            try:
                clean=reviews._proposal({k:suggestion.get(k) for k in ('kind','alias','product_id')},payload,catalog)
                proposals.append((row['id'],clean))
            except ValueError:continue
        # Reuse saved local recognition only. Do not reparse/replay all source
        # orders or mistake an old failure for validated grammar. These are
        # source references, never committed observations or auto-approvals.
        snapshot=payload.get('local_result')
        if row['outcome']=='RECOGNIZED' and isinstance(snapshot,dict) and snapshot.get('items'):
            info=dict(snapshot,raw_text=raw)
            grammar += [(row['id'],kind,form) for kind,form in forms_from_validated_order(info)]
    counts={'records':len(records),'aliases_created':0,'alias_sources':0,'rule_sources':0,'rules_created':0}
    links={r['id']:set() for r in records}
    from learning_knowledge import _identity,_principal
    from rule_dictionary import builtins
    def change(store):
        now=datetime.now(timezone.utc).isoformat()
        for identity,p in proposals:
            if p['kind']!='FLAVOR_ALIAS':continue
            from flavor_aliases import flavor_token
            product=next((item for item in catalog if item['id']==p['product_id']),None)
            if not product:continue
            p=dict(p,alias=flavor_token(p['alias'],product,catalog,store))
            if p['alias']==p['standard']:continue
            kind='EXPLICIT_FLAVOR' if p['kind']=='FLAVOR_ALIAS' else 'MODEL_ALIASES'
            approved=[r for r in store.get('aliases',[])+builtins() if r.get('status')=='active'
                and r.get('type')==kind and r.get('alias','').casefold()==p['alias'].casefold()
                and r.get('standard')==p['standard'] and (kind=='MODEL_ALIASES' or r.get('category')==p['category'])]
            if approved:
                links[identity].add('existing_alias');continue
            rows=store.setdefault('brand_candidates',[])
            matching=next((r for r in rows if r.get('type','MODEL_ALIASES')==kind and
                r.get('alias','').casefold()==p['alias'].casefold() and r.get('standard')==p['standard']
                and r.get('category_id')==p['category_id']),None)
            if matching is None:
                key=[kind,p['alias'].casefold(),p['standard'],p['category_id']]
                matching=dict(id='organized-'+hashlib.sha256(str(key).encode()).hexdigest()[:24],type=kind,
                    alias=p['alias'],standard=p['standard'],category=p['category'],product_id=p['product_id'],
                    category_id=p['category_id'],warehouse_id=p['warehouse_id'],scope='CATEGORY',status='pending',
                    count=0,confidence=0,first_seen=now,last_seen=now,origin='LOCAL_REVIEW_ORGANIZATION')
                rows.append(matching);counts['aliases_created']+=1
            sources=matching.setdefault('recognition_review_sources',[])
            if identity not in sources:sources.append(identity);matching['count']=max(matching.get('count',0),len(sources))
            links[identity].add(matching['id']);counts['alias_sources']+=1
        for row in store.get('brand_candidates',[]):
            if row.get('status') not in ('pending','conflict'):continue
            if any(other.get('status') in ('pending','conflict') and other.get('id')!=row.get('id')
                   and other.get('type','MODEL_ALIASES')==row.get('type','MODEL_ALIASES')
                   and other.get('alias','').casefold()==row.get('alias','').casefold()
                   and (row.get('type','MODEL_ALIASES')=='MODEL_ALIASES' or other.get('category')==row.get('category'))
                   and other.get('standard')!=row.get('standard') for other in store.get('brand_candidates',[])):
                row['status']='conflict'
        for identity,kind,form in grammar:
            scope='USER' if kind=='HABIT' else 'GLOBAL'
            principal=principals.get(identity)
            saved=next((reviews.unpack(r['ciphertext']).get('organization_principal') for r in records if r['id']==identity),None)
            if scope=='USER' and principal is None and not saved:continue
            principal_digest=(_principal(principal) if principal is not None else saved) if scope=='USER' else None
            key=_identity(kind,form,scope,principal_digest)
            rows=store.setdefault('learning_candidates',[])
            row=next((r for r in rows if r['id']==key),None)
            if row is None:
                row=dict(id=key,kind=kind,form=form,scope=scope,principal_digest=principal_digest,status='pending',
                    revision=1,count=0,observations=[],created_at=now,updated_at=now,origin='RECOGNITION_ONLY')
                rows.append(row);counts['rules_created']+=1
            sources=row.setdefault('recognition_review_sources',[])
            if identity not in sources:sources.append(identity)
            links[identity].add(key);counts['rule_sources']+=1
        if records:store.setdefault('audit',[]).append(dict(action='organize_recognition_reviews',actor='admin-request',at=now,**counts))
        return counts
    transaction(change)
    # Dictionary is durable before hiding sources. Interrupted reruns dedupe
    # by source IDs; never delete encrypted evidence or access audit/history.
    with reviews.database() as connection:
        for row in records:
            payload=reviews.unpack(row['ciphertext'])
            payload['organization']={'destinations':sorted(links[row['id']]),'at':datetime.now(timezone.utc).isoformat()}
            connection.execute("UPDATE recognition_reviews SET status=?,ciphertext=?,revision=revision+1 WHERE id=? AND revision=?",
                ('organized' if links[row['id']] else 'archived',reviews.pack(payload),row['id'],row['revision']))
        counts['organized']=sum(bool(v) for v in links.values());counts['archived']=len(records)-counts['organized']
    return counts
