"""Catalog-validated flavor aliases in the existing official dictionary store."""
import copy
import hashlib
import json
import secrets
from datetime import datetime, timezone
import brand_aliases as brands
import rule_dictionary as dictionary


def _mapping_key(row):
    import unicodedata
    norm=lambda v:unicodedata.normalize('NFKC',str(v or '')).strip().casefold()
    return (norm(row.get('alias')),norm(row.get('category')),norm(row.get('standard')),
            row.get('scope') or 'SCOPED',row.get('warehouse_id'),row.get('category_id'),row.get('product_id'))


def mapping_rejected(store, row):
    """A denial is a mapping and scope, never a global ban on a flavor name."""
    target=dict(parent_name=row.get('category'),parent_id=row.get('category_id'),
                default_warehouse_id=row.get('warehouse_id'))
    for rejected in (store.get('rejected_flavor_mappings',[]) + store.get('aliases',[]) + store.get('brand_candidates',[])):
        if (rejected.get('type')!='EXPLICIT_FLAVOR' or rejected.get('status') not in ('rejected','ignored')
                or rejected.get('rejection_revoked_at')):continue
        if _mapping_key(rejected)[:3] != _mapping_key(row)[:3]:continue
        if brands.scope_matches(rejected,target):return True
    return False


def _remember_rejection(store,row,actor,now):
    rows=store.setdefault('rejected_flavor_mappings',[])
    record=next((r for r in rows if _mapping_key(r)==_mapping_key(row)),None)
    if record is None:
        record={k:row.get(k) for k in ('type','alias','standard','category','scope','warehouse_id','category_id','product_id')}
        rows.append(record)
    record.update(status='rejected',rejected_by=actor,rejected_at=now,rejection_revoked_at=None)


def _restore_rejection(store,row,actor,now):
    if row.get('status') not in ('rejected','ignored'):
        raise ValueError('仅可明确撤销已拒绝的映射')
    for key in ('rejected_flavor_mappings','aliases','brand_candidates'):
        for record in store.get(key,[]):
            if _mapping_key(record)==_mapping_key(row):
                record.update(rejection_revoked_at=now,rejection_revoked_by=actor)
    row['status']='pending'


def _syntax_contaminated(alias, flavor_names=()):
    import re
    import unicodedata
    alias=unicodedata.normalize('NFKC',alias).strip()
    units=r'(?:支|枝|根|个|只|盒|箱|包|套|袋|瓶|颗|件|条|枚|罐)'
    number=r'[0-9零一二两三四五六七八九十百]+'
    leading=re.match(r'^'+number+r'\s*(.+)$',alias)
    leading_quantity=bool(leading and any(
        unicodedata.normalize('NFKC',str(name)).casefold()==leading[1].strip().casefold()
        for name in flavor_names))
    # Count+unit is syntax in any position, including 一个荔枝 / 荔枝(一盒).
    # Bare 枝 in 荔枝 is lexical, not a packaging suffix. Exact catalog names
    # retain priority at the callers, including legitimate numeric names.
    return bool(re.search(r'备选|备用|首选|缺货|随机|替换|备注|合计|总计|共计',alias)
        or re.search(number+r'\s*'+units,alias)
        or re.search(r'^'+units+r'(?:装)?(?:\s+|[:：])',alias)
        or leading_quantity
        or re.search(r'^\d+\s*(?:[xX×*]|\s)\s*\S',alias)
        or re.search(number+r'\s*[)\]】]*$',alias)
        or re.search(r'(?:支|(?<!荔)枝|根|个|只|盒|箱|包|套|袋|瓶|颗|件|条|枚|罐)(?:装)?\s*[)\]】]*$',alias))


def _brand_contaminated(alias, catalog, store):
    known={str(p.get('parent_name')) for p in catalog if p.get('parent_name')}
    known.update(str(r['alias']) for r in store.get('aliases',[])
        if r.get('type')=='MODEL_ALIASES' and r.get('status')=='active' and r.get('alias'))
    return any(alias.casefold().startswith(prefix.casefold()) for prefix in known)


def target(body, catalog):
    warehouse, category, product = (int(body[k]) for k in ('warehouse_id', 'category_id', 'product_id'))
    found = [p for p in catalog if p['id'] == product and p.get('parent_id') == category
             and p.get('default_warehouse_id') == warehouse]
    if len(found) != 1:
        raise ValueError('口味必须属于所选仓库和分类的真实商品目录')
    return found[0]


def overlapping(row, product):
    return row.get('category') == product['parent_name'] and brands.scope_matches(row, product)


def plan(body, catalog, store):
    product = target(body, catalog)
    raw = body.get('aliases')
    if not isinstance(raw, str) or len(raw) > 20000:
        raise ValueError('请填写一行一个口味叫法')
    aliases, seen, duplicates = [], set(), 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        alias = brands.clean_alias(line)
        if alias.casefold() in seen:
            duplicates += 1
        else:
            aliases.append(alias)
            seen.add(alias.casefold())
    if not 1 <= len(aliases) <= 200:
        raise ValueError('请填写1–200个口味叫法')
    new, existing, conflicts = [], [], []
    # Pending manual rows reserve a name until review, but never enter Parser.
    rows = [r for r in store['aliases'] + dictionary.builtins()
            if r.get('status') in ('active', 'pending')]
    standards = {p['name'] for p in catalog if p.get('parent_name') == product['parent_name']}
    for alias in aliases:
        matches = [r for r in rows if r['type'] == 'EXPLICIT_FLAVOR'
                   and r['alias'].casefold() == alias.casefold() and overlapping(r, product)]
        proposal=dict(type='EXPLICIT_FLAVOR',alias=alias,standard=product['name'],category=product['parent_name'],
                      warehouse_id=product['default_warehouse_id'],category_id=product['parent_id'],product_id=product['id'])
        if mapping_rejected(store,proposal):
            conflicts.append(dict(alias=alias,reason='该范围内的相同映射已拒绝，请先明确撤销拒绝'))
        elif _syntax_contaminated(alias,standards) and alias not in standards:
            conflicts.append(dict(alias=alias,reason='叫法混有首备选、数量或包装语法，请先分离'))
        elif alias not in standards and _brand_contaminated(alias,catalog,store):
            conflicts.append(dict(alias=alias,reason='叫法混有品牌或品类，请先分离'))
        elif alias in standards:
            conflicts.append(dict(alias=alias, reason='目录标准口味优先，不能作为其他口味叫法'))
        elif any(r['standard'] != product['name'] for r in matches):
            conflicts.append(dict(alias=alias, reason='同一分类范围叫法冲突'))
        elif matches:
            existing.append(alias)
        else:
            new.append(alias)
    result = dict(warehouse_id=product['default_warehouse_id'], category_id=product['parent_id'],
                  product_id=product['id'], standard=product['name'], category=product['parent_name'],
                  aliases=aliases, new=new, existing=existing, conflicts=conflicts, duplicates=duplicates)
    result['token'] = hashlib.sha256(json.dumps([result, store['aliases']], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return result


def preview(body, catalog):
    return plan(body, catalog, dictionary.read_store())


def save(body, actor, catalog):
    def change(store):
        result = plan(body, catalog, store)
        if result['conflicts']:
            raise ValueError('存在冲突，整批未保存')
        if not secrets.compare_digest(str(body.get('token', '')), result['token']):
            raise ValueError('目录或叫法已变化，请重新预览')
        now = datetime.now(timezone.utc).isoformat()
        for alias in result['new']:
            row = dict(id='manual-' + secrets.token_hex(8), type='EXPLICIT_FLAVOR', alias=alias,
                       standard=result['standard'], category=result['category'], model=result['category'],
                       warehouse_id=result['warehouse_id'], category_id=result['category_id'],
                       product_id=result['product_id'], status='pending', readonly=False,
                       source='人工口味叫法', mode='分类范围精确匹配', created_by=actor,
                       created_at=now, updated_at=now, reviewed_by=None, reviewed_at=None)
            store['aliases'].append(row)
            store['audit'].append(dict(actor=actor, action='flavor_create', id=row['id'], at=now))
        return dict(created=len(result['new']), existing=len(result['existing']), status='pending')
    return brands.transaction(change)


def mutate(body, actor, catalog):
    def change(store):
        row = next((r for r in store['aliases'] if r['id'] == body.get('id')
                    and r['type'] == 'EXPLICIT_FLAVOR' and not r.get('readonly')), None)
        if row is None:
            raise ValueError('人工口味叫法不存在')
        now = datetime.now(timezone.utc).isoformat()
        action = body.get('action')
        if body.get('expected_pending') and row.get('status')!='pending':
            raise ValueError('此叫法已处理，请刷新后查看')
        if action == 'delete':
            if row.get('status')=='rejected':_remember_rejection(store,row,actor,now)
            store['aliases'].remove(row)
        elif action == 'disable':
            row['status'] = 'disabled'
        elif action == 'reject':
            if row.get('status')!='pending':raise ValueError('只能拒绝待审核叫法')
            row.update(status='rejected',reviewed_by=actor,reviewed_at=now)
            _remember_rejection(store,row,actor,now)
        elif action == 'restore':
            _restore_rejection(store,row,actor,now)
        elif action == 'approve':
            product = target(row, catalog)
            if product['name'] != row['standard'] or product['parent_name'] != row['category']:
                raise ValueError('目录已变化，请重新维护叫法')
            check = plan(dict(row, aliases=row['alias']), catalog, store)
            if check['conflicts']:
                raise ValueError('同一分类叫法冲突，不能启用')
            row.update(status='active', reviewed_by=actor, reviewed_at=now)
        else:
            raise ValueError('操作无效')
        row['updated_at'] = now
        store['audit'].append(dict(actor=actor, action='flavor_' + action, id=row['id'], at=now))
        return row
    return brands.transaction(change)


def flavor_token(alias, product, catalog, store=None):
    """Separate a proven category prefix from the flavor; never infer a category."""
    import re
    alias=brands.clean_alias(alias)
    if alias==product.get('name'):return alias
    store=store if store is not None else dictionary.runtime_store()
    prefixes={p.get('parent_name') for p in catalog if p.get('parent_name')==product.get('parent_name')}
    prefixes.update(r.get('alias') for r in store.get('aliases',[]) if r.get('type')=='MODEL_ALIASES'
        and r.get('status')=='active' and r.get('standard')==product.get('parent_name') and brands.scope_matches(r,product))
    for prefix in sorted(filter(None,prefixes),key=len,reverse=True):
        match=re.match(r'^'+re.escape(prefix)+r'[\s:：/]+(.+)$',alias,re.I)
        if match:return match.group(1).strip()
    return alias

def safe_learning_token(alias,product,catalog,store=None,*,confirmed=False):
    """Reject contaminated or weak AI pairs before writing learning history."""
    import re
    from difflib import SequenceMatcher
    store=store if store is not None else dictionary.runtime_store()
    name=str(product['name'])
    if isinstance(alias,str) and alias.strip()==name:return name
    alias=brands.clean_alias(alias)
    if alias==name:return alias
    if _syntax_contaminated(alias,(p.get('name','') for p in catalog)):return None
    proposal=dict(type='EXPLICIT_FLAVOR',alias=alias,standard=name,category=product['parent_name'],
                  warehouse_id=product['default_warehouse_id'],category_id=product['parent_id'],product_id=product['id'])
    if mapping_rejected(store,proposal):return None
    # A complete standard flavor in another category is not evidence for
    # teaching a shorter/different flavor here (even if AI says confident).
    if any(p.get('parent_name')==product.get('parent_name')
           and str(p.get('name') or '').casefold()==alias.casefold()
           and str(p.get('name') or '').casefold()!=name.casefold() for p in catalog):
        return None
    # A connector is syntax only when its remainder is the exact target.
    connector=re.fullmatch(r'(?:和|与|以及)\s+(.+)',alias)
    if connector and connector[1].strip()==name:return name
    if _brand_contaminated(alias,catalog,store):
        # Whole raw brand+flavor strings are parsing evidence, not reusable
        # flavor aliases, regardless of which category AI selected.
        return None
    alias=flavor_token(alias,product,catalog,store)
    compact=lambda v:re.sub(r'\s+','',v).casefold()
    source,target=compact(alias),compact(name)
    if not source or not target:return None
    if not confirmed and source not in target and target not in source and SequenceMatcher(None,source,target).ratio()<.8:return None
    return alias

def learn(alias, product, confidence, catalog, origin='AI',*,event=None,confirmation=None):
    """Only catalog tokens, never order text or recipient fields."""
    if origin!='CUSTOMER_CONFIRMED_AI' or not event or not isinstance(confirmation,dict):return None
    import re
    if (confirmation.get('category')!=product.get('parent_name')
            or confirmation.get('standard')!=product.get('name')
            or confirmation.get('raw_flavor')!=alias
            or product.get('id') not in confirmation.get('product_ids',[])
            or not re.fullmatch(r'[a-f0-9]{8}',str(confirmation.get('revision','')))
            or not all(re.fullmatch(r'[a-f0-9]{64}',str(confirmation.get(k,''))) for k in ('callback_ref','source_ref'))):return None
    target(dict(warehouse_id=product['default_warehouse_id'], category_id=product['parent_id'],
                product_id=product['id']), catalog)
    alias = safe_learning_token(alias,product,catalog,confirmed=True)
    if alias is None:return None
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise ValueError('置信信息无效')
    if origin not in ('AI','CUSTOMER_CONFIRMED_AI'):
        raise ValueError('学习来源无效')
    if alias.casefold() == product['name'].casefold():
        return None
    def change(store):
        proposal=dict(type='EXPLICIT_FLAVOR',alias=alias,standard=product['name'],category=product['parent_name'],
                      warehouse_id=product['default_warehouse_id'],category_id=product['parent_id'],product_id=product['id'])
        if mapping_rejected(store,proposal):return None
        # An approved mapping already covers this observation. Never enqueue
        # it again, and do not let an AI proposal override its target.
        approved = [r for r in store['aliases'] + dictionary.builtins()
                    if r.get('status') == 'active'
                    and r.get('type') == 'EXPLICIT_FLAVOR'
                    and r['alias'].casefold() == alias.casefold()
                    and overlapping(r, product)]
        if approved:
            return None
        now = datetime.now(timezone.utc).isoformat()
        scope = (product['default_warehouse_id'], product['parent_id'])
        rows = [r for r in store['brand_candidates'] if r.get('type') == 'EXPLICIT_FLAVOR'
                and ((r.get('scope')=='GLOBAL' and r.get('category')==product['parent_name']) or (r['warehouse_id'], r['category_id']) == scope)
                and r['alias'].casefold() == alias.casefold()]
        row = next((r for r in rows if r['standard'] == product['name']), None)
        if row:
            if event and event in row.get('commit_observations',[]):return row
            row.update(count=row['count']+1, last_seen=now, confidence=confidence)
        else:
            row = dict(id='candidate-'+secrets.token_hex(8), type='EXPLICIT_FLAVOR', alias=alias,
                       standard=product['name'], category=product['parent_name'], product_id=product['id'],
                       warehouse_id=scope[0], category_id=scope[1], count=1, first_seen=now,
                       last_seen=now, confidence=confidence, status='pending')
            store['brand_candidates'].append(row)
        if event:row.setdefault('commit_observations',[]).append(event)
        row['origin']=origin
        proof=dict(confirmation,category_id=product['parent_id'],product_id=product['id'],commit_fingerprint=event)
        if proof not in row.setdefault('customer_confirmations',[]):row['customer_confirmations'].append(proof)
        if origin=='CUSTOMER_CONFIRMED_AI':
            row['confirmation_count']=int(row.get('confirmation_count') or 0)+1
        for other in rows:
            if other['standard'] != row['standard'] and other['status'] in ('pending','conflict','approved'):
                other['status'] = row['status'] = 'conflict'
        return row
    return brands.transaction(change)


def review(body, actor, catalog, *, _store=None):
    def change(store):
        row = next((r for r in store['brand_candidates'] if r['id'] == body.get('id')
                    and r.get('type') == 'EXPLICIT_FLAVOR'), None)
        if not row:
            raise ValueError('口味候选不存在')
        if body.get('_expected_pending') and row.get('status') not in ('pending','conflict'):
            raise ValueError('候选已处理，请刷新后重试')
        previous_status=row['status']
        action = body.get('action')
        now = datetime.now(timezone.utc).isoformat()
        if action == 'approve':
            if not row.get('customer_confirmations'):
                raise ValueError('该口味映射缺少客户按钮确认依据，不能审核生效；原历史记录保持不变')
            if row['status'] not in ('pending','conflict'):
                raise ValueError('候选已处理')
            if any(r['id'] != row['id'] and r.get('type') == 'EXPLICIT_FLAVOR'
                   and r['alias'].casefold() == row['alias'].casefold()
                   and ((r.get('scope')=='GLOBAL' and r.get('category')==row.get('category')) or (r['warehouse_id'] == row['warehouse_id'] and r['category_id'] == row['category_id']))
                   and r['standard'] != row['standard'] and r['status'] in ('pending','conflict','approved')
                   for r in store['brand_candidates']):
                raise ValueError('口味候选冲突，请先人工判断')
            result = plan(dict(row, aliases=row['alias']), catalog, store)
            if result['standard'] != row['standard'] or result['conflicts']:
                raise ValueError('目录已变化或叫法冲突')
            if result['new']:
                new_alias_id='manual-'+secrets.token_hex(8)
                row['created_alias_ids']=[new_alias_id]
                store['aliases'].append(dict(row,id=new_alias_id,status='active',
                    readonly=False,source='AI口味候选人工审核',mode='分类范围精确匹配',
                    created_by=actor,created_at=now,reviewed_by=actor,reviewed_at=now,updated_at=now,
                    model=row['category']))
            else:
                row['review_note']='相同映射已存在；仅登记候选审核，不新增重复叫法'
                row['existing_alias_ids']=[r['id'] for r in store['aliases'] if r.get('type')=='EXPLICIT_FLAVOR'
                    and r.get('status') in ('active','pending') and r['alias'].casefold()==row['alias'].casefold()
                    and r['standard']==row['standard'] and overlapping(r,target(row,catalog))]
            row['status']='approved'
        elif action == 'revoke':
            brands.revoke_candidate_mapping(row,store)
        elif action in ('ignore','reject'):
            row['status']='ignored' if action=='ignore' else 'rejected'
            _remember_rejection(store,row,actor,now)
        elif action == 'restore':
            _restore_rejection(store,row,actor,now)
        elif action == 'delete':
            if row.get('status') in ('ignored','rejected'):_remember_rejection(store,row,actor,now)
            store['brand_candidates'].remove(row)
        else:
            raise ValueError('操作无效')
        brands.record_review_version(row,previous_status,action,actor,now)
        row.update(reviewed_by=actor,reviewed_at=now)
        store['audit'].append(dict(actor=actor,action='flavor_candidate_'+action,id=row['id'],at=now))
        return row
    return change(_store) if _store is not None else brands.transaction(change)


def candidate_batch_plan(body,catalog,store):
    ids=body.get('ids');action=body.get('action')
    if (action not in ('approve','delete') or not isinstance(ids,list) or not 1<=len(ids)<=200
            or any(not isinstance(i,str) or not i for i in ids) or len(set(ids))!=len(ids)):
        raise ValueError('请选择1至200条不同口味候选和有效操作')
    rows=[r for r in store.get('brand_candidates',[]) if r.get('id') in ids]
    if len(rows)!=len(ids) or any(r.get('type')!='EXPLICIT_FLAVOR' for r in rows):
        raise ValueError('批量操作仅支持当前真实口味候选')
    identity=[action,sorted(ids),rows,store.get('aliases',[]),catalog]
    token=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    # Validate every selected row independently on a private snapshot.  A
    # stale or conflicting candidate is reported, while unrelated valid rows
    # remain eligible for this batch.
    simulated=copy.deepcopy(store);planned=[];skipped=[]
    for identity_value in ids:
        try:
            review(dict(id=identity_value,action=action),'batch-preview',catalog,_store=simulated)
            planned.append(identity_value)
        except ValueError as error:
            skipped.append(dict(id=identity_value,reason=str(error)))
    return dict(count=len(rows),eligible=len(planned),action=action,token=token,
                planned_ids=planned,skipped=skipped)


def candidate_batch_preview(body,catalog):
    return candidate_batch_plan(body,catalog,dictionary.read_store())


def candidate_batch_review(body,actor,catalog):
    def change(store):
        result=candidate_batch_plan(body,catalog,store)
        if not secrets.compare_digest(str(body.get('token','')),result['token']):
            raise ValueError('候选、目录或Alias已变化，请重新预览')
        processed=[];skipped=[]
        for identity_value in body['ids']:
            try:
                review(dict(id=identity_value,action=body['action']),actor,catalog,_store=store)
                processed.append(identity_value)
            except ValueError as error:
                skipped.append(dict(id=identity_value,reason=str(error)))
        store.setdefault('audit',[]).append(dict(
            actor=actor,action='flavor_candidate_batch_'+body['action'],
            requested=len(body['ids']),processed=len(processed),skipped=len(skipped),
            at=datetime.now(timezone.utc).isoformat()))
        return dict(processed=len(processed),processed_ids=processed,
                    skipped=skipped,partial=bool(processed and skipped))
    return brands.transaction(change)
