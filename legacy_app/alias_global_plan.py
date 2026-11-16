"""Reversible, catalog-ID-based proposal; never auto-approves ambiguous rows."""
import copy
import unicodedata

def fingerprint(store):
    import hashlib, json
    canonical=copy.deepcopy(store)
    for key in ('aliases','brand_candidates','audit'):canonical.setdefault(key,[])
    return hashlib.sha256(json.dumps(canonical,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def apply(catalog, actor, expected_fingerprint):
    """Locked conversion with a fresh snapshot guard; caller retains backup."""
    import hashlib, json
    from datetime import datetime, timezone
    from brand_aliases import transaction
    def change(store):
        digest=fingerprint(store)
        if digest != expected_fingerprint:
            raise ValueError('Alias数据已变化，请重新审计，不覆盖新记录')
        converted=plan(store,catalog)
        before=copy.deepcopy(store)
        for key in ('aliases','brand_candidates'):
            store[key]=converted['store'].get(key,[])
        now=datetime.now(timezone.utc).isoformat()
        store.setdefault('audit',[]).append(dict(action='global_scope_conversion',actor=actor,at=now,
            unresolved=converted['unresolved'],conflicts=converted['conflicts']))
        store['updated_at']=now
        return dict(before=before,converted=copy.deepcopy(store),unresolved=converted['unresolved'],conflicts=converted['conflicts'])
    return transaction(change)

def rollback(current, before, converted):
    """Undo only unchanged converted rows; preserve later live edits and new rows."""
    result = copy.deepcopy(current)
    conflicts = []
    for key in ('aliases', 'brand_candidates'):
        old = {r['id']: r for r in before.get(key, [])}
        new = {r['id']: r for r in converted.get(key, [])}
        for index, row in enumerate(result.get(key, [])):
            rid = row.get('id')
            if rid not in old or rid not in new or old[rid] == new[rid]:
                continue
            if row != new[rid]:
                conflicts.append(rid)
                continue
            result[key][index] = copy.deepcopy(old[rid])
    return {'store': result, 'conflicts': conflicts}

def plan(store, catalog):
    result=copy.deepcopy(store)
    by_id={p['id']:p for p in catalog}
    unresolved=[]
    for key in ('aliases','brand_candidates'):
        for row in result.get(key,[]):
            # Quarantined built-ins are not a migration source.
            if row.get('readonly') or row.get('status') in ('quarantined','archived','deleted'):continue
            product=by_id.get(row.get('product_id'))
            category=by_id.get(row.get('category_id'))
            if product and product.get('parent_id'):
                parent=by_id.get(product['parent_id'])
                if not parent or parent.get('is_archived') or product.get('is_archived'):
                    unresolved.append(row.get('id'));continue
                if row.get('standard')!=product['name']:
                    unresolved.append(row.get('id'));continue
                row.update(type='EXPLICIT_FLAVOR',category=parent['name'],standard=product['name'],scope='GLOBAL')
            elif category and category.get('is_parent') and not category.get('is_archived') and row.get('standard')==category['name']:
                row.update(type='MODEL_ALIASES',scope='GLOBAL')
            else:unresolved.append(row.get('id'))
    groups={}
    for row in result.get('aliases',[]):
        if row.get('scope')!='GLOBAL' or row.get('status') not in ('active','pending'):continue
        key=(row['type'],unicodedata.normalize('NFKC',row['alias']).strip().casefold(),row.get('category') if row['type']=='EXPLICIT_FLAVOR' else None)
        groups.setdefault(key,[]).append(row)
    conflicts=[]
    for rows in groups.values():
        if len({r['standard'] for r in rows})>1:
            for row in rows:
                row['status']='pending';row['global_review_required']=True
                conflicts.append(row['id'])
    for key in ('aliases','brand_candidates'):
        for row in result.get(key,[]):
            if row.get('id') in unresolved and row.get('status') in ('active','pending','conflict'):
                row['status']='pending';row['global_review_required']=True
    return {'store':result,'unresolved':unresolved,'conflicts':conflicts}
