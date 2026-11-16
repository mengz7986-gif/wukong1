"""Reviewed brand aliases and privacy-minimized learning candidates.

Uses the existing dictionary data layer, not a second parser or rule system.
"""
import fcntl
import json
import os
import re
import secrets
import hashlib
import tempfile
import unicodedata
from datetime import datetime, timezone
import rule_dictionary as dictionary


def clean_alias(value):
    if not isinstance(value, str):
        raise ValueError('请填写客户品牌叫法')
    value=value.strip()
    if not 1 <= len(value) <= 40 or any(
        unicodedata.category(c).startswith('C') and c != '\u200d' for c in value
    ) or any(c in '<>\n\r\t' for c in value) or not any(
        c.isalnum() or unicodedata.category(c) == 'So' for c in value
    ):
        raise ValueError('叫法须为文字、数字或Emoji，不能包含控制字符')
    if re.search(r'\d{7}|姓名|收件|电话|手机|地址|先生|女士|街道|小区|\d+[号栋室]',value):
        raise ValueError('不能保存客户收件信息作为品牌叫法')
    return value


def batch_plan(body, catalog, store):
    """Newline is the only separator. Existing rows and approval states survive."""
    if not isinstance(body,dict) or not isinstance(body.get('aliases'),str):
        raise ValueError('请填写一行一个其他叫法')
    if len(body['aliases'])>20000: raise ValueError('叫法文本过长')
    warehouse,category=int(body['warehouse_id']),int(body['category_id'])
    standards={p['parent_name'] for p in catalog if p.get('parent_id')==category
               and p.get('default_warehouse_id')==warehouse}
    if len(standards)!=1: raise ValueError('当前范围没有唯一真实标准名称')
    standard=next(iter(standards));aliases=[];seen=set();duplicates=0
    standard_names={p['parent_name'].casefold(): p['parent_name'] for p in catalog}
    for line in body['aliases'].splitlines():
        if not line.strip():continue
        alias=clean_alias(line)
        if alias.casefold() in seen:duplicates+=1;continue
        seen.add(alias.casefold());aliases.append(alias)
    if not aliases or len(aliases)>200:raise ValueError('请填写1–200个其他叫法')
    existing=[];new=[];conflicts=[]
    for alias in aliases:
        if alias.casefold() in standard_names:
            if standard_names[alias.casefold()] == standard:
                existing.append(alias)
            else:
                conflicts.append({'alias':alias,'reason':'当前标准名称已占用：'+standard_names[alias.casefold()]})
            continue
        incompatible=[r for r in store['aliases']+dictionary.builtins()
            if r.get('status') in ('active','pending') and r['type']=='MODEL_ALIASES' and r['alias'].casefold()==alias.casefold()
            and r['standard']!=standard
            ]
        if incompatible:
            conflicts.append({'alias':alias,'reason':'同一有效范围存在不同标准名称，请先处理冲突'})
            continue
        exact=[r for r in store['aliases'] if r['type']=='MODEL_ALIASES'
               and r['alias'].casefold()==alias.casefold() and r['standard']==standard
               and (r.get('scope')=='GLOBAL' or (r.get('warehouse_id')==warehouse and r.get('category_id')==category))]
        if exact:existing.append(alias);continue
        try:
            if alias.casefold()==standard.casefold():raise ValueError('已是标准名称，无需新增')
            conflicting(store,alias,standard,warehouse,category)
            new.append(alias)
        except ValueError as error:conflicts.append({'alias':alias,'reason':str(error)})
    plan=dict(warehouse_id=warehouse,category_id=category,standard=standard,
              aliases=aliases,new=new,existing=existing,conflicts=conflicts,duplicates=duplicates)
    # Changes to scope/rows after preview require another explicit preview.
    material={'plan':plan,'existing_rows':store['aliases']}
    plan['token']=hashlib.sha256(json.dumps(material,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return plan


def batch_preview(body,catalog):
    return batch_plan(body,catalog,dictionary.read_store())


def batch_save(body,actor,catalog):
    def change(store):
        plan=batch_plan(body,catalog,store)
        if plan['conflicts']:raise ValueError('存在冲突，整批未保存，请修改后重新预览')
        if not secrets.compare_digest(str(body.get('token','')),plan['token']):
            raise ValueError('目录或叫法已变化，请重新预览后保存')
        now=datetime.now(timezone.utc).isoformat()
        for alias in plan['new']:
            row=dict(id='manual-'+secrets.token_hex(8),type='MODEL_ALIASES',alias=alias,
                standard=plan['standard'],category=plan['standard'],model=plan['standard'],
                warehouse_id=plan['warehouse_id'],category_id=plan['category_id'],
                readonly=False,source='人工批量新增',mode='分类范围精确匹配',status='pending',
                created_by=actor,created_at=now,updated_at=now,reviewed_by=None,reviewed_at=None)
            store['aliases'].append(row)
            store['audit'].append(dict(actor=actor,action='batch_create',id=row['id'],at=now))
        store['updated_at']=now
        return dict(created=len(plan['new']),existing=len(plan['existing']),status='pending')
    return transaction(change)


def transaction(change):
    target=dictionary.path();target.parent.mkdir(parents=True,exist_ok=True)
    with open(str(target)+'.lock','a') as lock:
        os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        store=dictionary.read_store();store.setdefault('brand_candidates',[])
        existing_ids={r.get('id') for key in ('aliases','brand_candidates') for r in store.get(key,[])}
        result=change(store)
        for key in ('aliases','brand_candidates'):
            for row in store.get(key,[]):
                if row.get('id') not in existing_ids and not row.get('readonly'):
                    row['scope']='GLOBAL'
                    if key=='brand_candidates':row.setdefault('type','MODEL_ALIASES')
        fd,temporary=tempfile.mkstemp(dir=target.parent,prefix='.brands-')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stream.write(json.dumps(store,ensure_ascii=False));stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,0o600);os.replace(temporary,target)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
        return result


def scope_matches(row,product):
    if row.get('scope')=='GLOBAL':
        return row.get('type')!='EXPLICIT_FLAVOR' or row.get('category')==product.get('parent_name')
    return (not row.get('warehouse_id') or int(row['warehouse_id'])==int(product.get('default_warehouse_id') or 0)) and (
        not row.get('category_id') or int(row['category_id'])==int(product.get('parent_id') or 0))


def scope_catalog(alias,products):
    """Known scoped aliases cannot leak into another warehouse/category."""
    rows=[r for r in dictionary.runtime_store()['aliases'] if r['type']=='MODEL_ALIASES'
          and r['status']=='active' and r['alias']==str(alias or '').strip()]
    if not rows:return None
    standards={r['standard'] for r in rows if any(scope_matches(r,p) and p.get('parent_name')==r['standard'] for p in products)}
    if len(standards)!=1:return []
    standard=next(iter(standards))
    return [p for p in products if p.get('parent_name')==standard and any(scope_matches(r,p) for r in rows if r['standard']==standard)]


def matched_reviewed_aliases(text,products):
    """Privacy-free proof that an active reviewed alias affected this parse."""
    source=str(text or '').casefold();result=[]
    for row in dictionary.runtime_store().get('aliases',[]):
        alias=str(row.get('alias') or '').strip()
        if (not alias or row.get('status')!='active' or row.get('readonly')
                or alias.casefold() not in source or not any(scope_matches(row,p) for p in products)):
            continue
        result.append({'id':row.get('id'),'kind':'ALIAS','scope':row.get('scope','GLOBAL'),'type':row.get('type')})
    return result


def conflicting(store,alias,standard,warehouse,category,exclude=None):
    from bundle_aliases import key, reserved_names
    if key(alias) in reserved_names() or any(r.get('type')=='BUNDLE_ALIASES'
            and r.get('status') in ('active','pending') and key(r.get('alias',''))==key(alias)
            for r in store['aliases']):
        raise ValueError('叫法与已有福袋名称或叫法冲突，不能覆盖福袋映射')
    for row in store['aliases']+dictionary.builtins():
        if row.get('status') not in ('active','pending') or row['id']==exclude or row['type']!='MODEL_ALIASES' or row['alias'].casefold()!=alias.casefold():continue
        # Newly inserted rows are GLOBAL (transaction applies that policy).
        # A scoped existing mapping elsewhere must not be silently overridden.
        approving_global=any(r.get('id')==exclude and r.get('scope')=='GLOBAL' for r in store['aliases'])
        overlap=exclude is None or approving_global or row.get('scope')=='GLOBAL' or ((not row.get('warehouse_id') or row['warehouse_id']==warehouse) and (not row.get('category_id') or row['category_id']==category))
        if overlap and (row['standard']!=standard or (exclude is None and not row['readonly'])):
            raise ValueError('同一范围叫法重复或冲突，不能覆盖已有品牌映射')


def protect_standard_name(alias, standard, warehouse, catalog):
    for product in catalog:
        name = product.get('parent_name') or ''
        if alias.casefold() == name.casefold() and name != standard:
            raise ValueError('当前标准名称已占用：' + name)


def validate_approval(row, store, catalog):
    protect_standard_name(row['alias'],row['standard'],row.get('warehouse_id'),catalog)
    products=[p for p in catalog if scope_matches(row,p) and p['parent_name']==row['standard']]
    if not products:raise ValueError('当前目录不存在对应品牌')
    from deepseek_resolver import enforce_generation
    resolved,_=enforce_generation(row['standard'],row['alias'],sorted({p['parent_name'] for p in catalog}),allow_reject=True)
    if resolved!=row['standard']:raise ValueError('叫法与既有代际保护规则冲突')
    conflicting(store,row['alias'],row['standard'],row.get('warehouse_id'),row.get('category_id'),row['id'])


def bulk_status(body, actor, catalog):
    """One locked, scoped review; invalid rows remain pending, never touch builtins."""
    if not isinstance(body, dict) or body.get('action') not in ('approve', 'disable'):
        raise ValueError('批量操作无效')
    warehouse, category = int(body['warehouse_id']), int(body['category_id'])
    product_id = int(body['product_id']) if body.get('product_id') else None
    products = [p for p in catalog if p.get('default_warehouse_id') == warehouse and p.get('parent_id') == category]
    if not products or len({p['parent_name'] for p in products}) != 1:
        raise ValueError('当前标准对象已失效')
    standard = products[0]['parent_name']
    if product_id:
        import flavor_aliases
        product = flavor_aliases.target(body, catalog)
        standard = product['name']
    kind = 'EXPLICIT_FLAVOR' if product_id else 'MODEL_ALIASES'
    action = body['action']; required = 'pending' if action == 'approve' else 'active'
    def change(store):
        rows = [r for r in store['aliases'] if not r.get('readonly')
                and str(r.get('source', '')).startswith('人工') and r.get('type') == kind
                and (r.get('scope')=='GLOBAL' or (r.get('warehouse_id') == warehouse and r.get('category_id') == category))
                and r.get('standard') == standard and r.get('status') == required
                and (not product_id or (r.get('scope')=='GLOBAL' and r.get('category')==product['parent_name']) or r.get('product_id') == product_id)]
        now = datetime.now(timezone.utc).isoformat()
        result = dict(attempted=len(rows), success=0, conflicts=[], failed=0)
        seen = set()
        for row in rows:
            try:
                if action == 'approve':
                    normalized = unicodedata.normalize('NFKC', clean_alias(row['alias'])).casefold()
                    if normalized in seen:raise ValueError('规范化后重复，保留待审核')
                    for other in store['aliases']:
                        if other['id'] == row['id'] or other.get('type') != kind or other.get('status') not in ('active', 'pending'):
                            continue
                        if not any(scope_matches(other,p) for p in products):continue
                        if product_id and other.get('category') != product['parent_name']:continue
                        if unicodedata.normalize('NFKC',other['alias']).casefold() == normalized and other.get('standard') != standard:
                            raise ValueError('规范化后被其他标准对象占用：'+other['standard'])
                    if product_id:
                        check = flavor_aliases.plan(dict(row, aliases=row['alias']), catalog, store)
                        if check['conflicts']:raise ValueError(check['conflicts'][0]['reason'])
                    else:validate_approval(row, store, catalog)
                    seen.add(normalized)
            except ValueError as error:
                result['conflicts'].append(dict(id=row['id'], alias=row['alias'], reason=str(error)))
                continue
            row.update(status='active' if action == 'approve' else 'disabled', updated_at=now)
            if action == 'approve':row.update(reviewed_by=actor, reviewed_at=now)
            store['audit'].append(dict(actor=actor, action='bulk_'+action, id=row['id'], at=now))
            result['success'] += 1
        store['updated_at'] = now
        store['audit'].append(dict(actor=actor, action='bulk_status_summary', at=now,
            warehouse_id=warehouse, category_id=category, product_id=product_id,
            attempted=result['attempted'], success=result['success'],
            conflicts=len(result['conflicts']), failed=result['failed']))
        return result
    return transaction(change)


def mutate(body,actor,catalog):
    if not isinstance(body,dict):raise ValueError('请求格式无效')
    def change(store):
        action=body.get('action');now=datetime.now(timezone.utc).isoformat()
        row=next((r for r in store['aliases'] if r['id']==body.get('id') and r['type']=='MODEL_ALIASES'),None)
        if body.get('expected_pending') and (not row or row.get('status')!='pending'):
            raise ValueError('此叫法已处理，请刷新后查看')
        if action=='create':
            warehouse,category=int(body['warehouse_id']),int(body['category_id'])
            products=[p for p in catalog if p.get('parent_id')==category and p.get('default_warehouse_id')==warehouse]
            standards={p['parent_name'] for p in products}
            if len(standards)!=1:raise ValueError('所选范围没有唯一真实标准品牌')
            standard=next(iter(standards));alias=clean_alias(body.get('alias'))
            protect_standard_name(alias,standard,warehouse,catalog)
            if alias.casefold()==standard.casefold():raise ValueError('叫法与标准品牌相同，无需新增')
            conflicting(store,alias,standard,warehouse,category)
            row=dict(id='manual-'+secrets.token_hex(8),type='MODEL_ALIASES',alias=alias,standard=standard,
                     category=standard,model=standard,warehouse_id=warehouse,category_id=category,
                     readonly=False,source='人工新增',mode='分类范围精确匹配',status='pending',
                     created_by=actor,created_at=now,reviewed_by=None,reviewed_at=None)
            store['aliases'].append(row)
        elif not row or row.get('readonly'):raise ValueError('人工叫法不存在或只读')
        elif action=='delete':store['aliases'].remove(row)
        elif action=='reject':
            if row.get('status')!='pending':raise ValueError('只能拒绝待审核叫法')
            row.update(status='rejected',reviewed_by=actor,reviewed_at=now)
        elif action in ('approve','disable'):
            if action=='approve':
                validate_approval(row,store,catalog)
                row.update(status='active',reviewed_by=actor,reviewed_at=now)
            else:row['status']='disabled'
        else:raise ValueError('操作无效')
        row['updated_at']=now;store['updated_at']=now
        store['audit'].append(dict(actor=actor,action=action,id=row['id'],at=now))
        return row
    return transaction(change)


def learn(alias,standard,warehouse,category,confidence,catalog,*,event=None):
    """Automatic classification learning is disabled; manual audited aliases remain supported."""
    return None


def review(body,actor,catalog):
    """Approval and alias insertion are atomic; ignored/deleted candidates never activate."""
    if any(r['id']==body.get('id') and r.get('type')=='EXPLICIT_FLAVOR'
           for r in dictionary.read_store().get('brand_candidates',[])):
        from flavor_aliases import review as flavor_review
        return flavor_review(body,actor,catalog)
    def change(store):
        row=next((r for r in store['brand_candidates'] if r['id']==body.get('id')),None)
        if not row:raise ValueError('学习候选不存在')
        if body.get('_expected_pending') and row.get('status') not in ('pending','conflict'):
            raise ValueError('候选已处理，请刷新后重试')
        previous_status=row['status']
        action=body.get('action');now=datetime.now(timezone.utc).isoformat()
        if action=='approve':
            if row['status'] not in ('pending','conflict'):raise ValueError('候选已处理')
            protect_standard_name(row['alias'],row['standard'],row['warehouse_id'],catalog)
            if any(r.get('type','MODEL_ALIASES')=='MODEL_ALIASES' and r['id']!=row['id'] and r['alias'].casefold()==row['alias'].casefold() and (r.get('scope')=='GLOBAL' or r['warehouse_id']==row['warehouse_id']) and r['standard']!=row['standard'] and r['status'] in ('pending','conflict','approved') for r in store['brand_candidates']):
                raise ValueError('候选品牌冲突，请先人工判断并忽略其他冲突候选')
            if not any(scope_matches(row,p) and p['parent_name']==row['standard'] for p in catalog):raise ValueError('目录品牌已不存在')
            from deepseek_resolver import enforce_generation
            resolved,_=enforce_generation(row['standard'],row['alias'],sorted({p['parent_name'] for p in catalog}),allow_reject=True)
            if resolved!=row['standard']:raise ValueError('候选与代际保护规则冲突')
            existing=[r for r in store['aliases'] if r.get('type')=='MODEL_ALIASES'
                and r.get('status')=='active' and r['alias'].casefold()==row['alias'].casefold()
                and r['standard']==row['standard'] and (r.get('scope')=='GLOBAL'
                    or (r.get('warehouse_id'),r.get('category_id'))==(row['warehouse_id'],row['category_id']))]
            if existing:
                if any(r.get('type')=='MODEL_ALIASES' and r.get('status') in ('active','pending')
                       and r['alias'].casefold()==row['alias'].casefold() and r['standard']!=row['standard']
                       for r in store['aliases']+dictionary.builtins()):
                    raise ValueError('同名叫法存在不同品牌映射，请先人工核对冲突')
                row.update(status='approved',existing_alias_ids=[r['id'] for r in existing],
                    review_note='相同映射已启用；仅登记候选审核，不新增重复叫法',reviewed_by=actor,reviewed_at=now)
                store['audit'].append(dict(actor=actor,action='candidate_existing_mapping',id=row['id'],at=now,
                    alias_ids=row['existing_alias_ids']))
                record_review_version(row,previous_status,action,actor,now)
                return row
            conflicting(store,row['alias'],row['standard'],row['warehouse_id'],row['category_id'])
            new_alias_id='manual-'+secrets.token_hex(8)
            row['created_alias_ids']=[new_alias_id]
            store['aliases'].append(dict(id=new_alias_id,type='MODEL_ALIASES',alias=row['alias'],standard=row['standard'],
                category=row['standard'],model=row['standard'],warehouse_id=row['warehouse_id'],category_id=row['category_id'],
                readonly=False,source='AI候选人工审核',mode='分类范围精确匹配',status='active',created_by=actor,created_at=now,
                reviewed_by=actor,reviewed_at=now,updated_at=now))
            row['status']='approved'
        elif action=='revoke':
            revoke_candidate_mapping(row,store)
        elif action=='ignore':row['status']='ignored'
        elif action=='delete':store['brand_candidates'].remove(row)
        else:raise ValueError('候选操作无效')
        record_review_version(row,previous_status,action,actor,now)
        row.update(reviewed_by=actor,reviewed_at=now);store['audit'].append(dict(actor=actor,action='candidate_'+action,id=row['id'],at=now))
        return row
    return transaction(change)


def record_review_version(row,previous_status,action,actor,now):
    version=int(row.get('review_version') or 0)+1
    row['review_version']=version
    row['review_history']=(row.get('review_history',[])+[dict(version=version,
        previous_status=previous_status,status=row.get('status'),action=action,actor=actor,at=now)])[-50:]


def revoke_candidate_mapping(row,store):
    """Withdraw only mappings created by this review, never reused mappings."""
    if row.get('status')!='approved':raise ValueError('仅已审核候选可撤销')
    if not row.get('created_alias_ids') and not row.get('existing_alias_ids'):
        raise ValueError('历史审核缺少映射归属证据，请在知识库逐项停用')
    ids=set(row.get('created_alias_ids') or [])
    targets=[alias for alias in store['aliases'] if alias.get('id') in ids]
    if len(targets)!=len(ids):raise ValueError('关联叫法已变化，请在知识库逐项审核')
    for alias in targets:
        if (alias.get('readonly') or alias.get('alias')!=row.get('alias')
                or alias.get('standard')!=row.get('standard')
                or not str(alias.get('source','')).startswith('AI')):
            raise ValueError('关联叫法已修改，不能自动撤销')
    for alias in targets:alias['status']='disabled'
    row['status']='revoked'


def learn_validated(ai_result,preview,catalog,*,confirmations=()):
    """Stage only validated product tokens; no learning before a real commit."""
    safe={key:ai_result[key] for key in ('confidence',) if key in ai_result}
    contacts=[str(s.get(k) or '') for s in (ai_result,preview) for k in ('recipient','recipient_name','phone','recipient_phone','address','recipient_address')]
    for group in ('items','alternates'):
        safe[group]=[]
        for item in ai_result.get(group) or []:
            if not isinstance(item,dict):continue
            copied={}
            for key in ('category','flavor','raw_category','raw_flavor','for'):
                if item.get(key):
                    token=str(item[key])
                    if key.startswith('raw_') and any(token and token in value for value in contacts):continue
                    try:copied[key]=clean_alias(token)
                    except ValueError:continue
            item_category=item.get('category')
            if group=='alternates' and not item_category:
                mains=[main for main in ai_result.get('items',[]) if item.get('for') in (main.get('flavor'),main.get('raw_flavor'))]
                if len(mains)==1:item_category=mains[0].get('category')
            proofs=[p for p in confirmations if p.get('category')==item_category
                    and p.get('raw_flavor')==item.get('raw_flavor') and p.get('standard')==item.get('flavor')]
            if len(proofs)==1:copied['_customer_confirmation']=dict(proofs[0])
            safe[group].append(copied)
    preview['_learning_evidence']={'result':safe,'catalog':[{k:p[k] for k in ('id','name','parent_name','parent_id','default_warehouse_id','is_active','is_archived') if k in p} for p in catalog]}


def _learn_from_committed(ai_result,preview,catalog,event):
    """Only selected brand tokens; never persist raw orders/contact/source_text."""
    contacts = [str(source.get(field) or '') for source in (ai_result, preview)
                for field in ('recipient','recipient_name','phone','recipient_phone','address','recipient_address')]
    for item in ai_result.get('items') or []:
        if not isinstance(item,dict):continue
        matches=[p for p in preview['items'] if p.get('parent_name')==item.get('category') and p.get('product_name')==item.get('flavor')]
        if len(matches)!=1:continue
        product=next((p for p in catalog if p['id']==matches[0]['product_id']),None)
        if product and item.get('raw_category'):
            alias=str(item['raw_category']).strip()
            if any(alias and alias in value for value in contacts):
                continue
            learn(item['raw_category'],product['parent_name'],product['default_warehouse_id'],product['parent_id'],ai_result.get('confidence',0),catalog,event=event)
        if product and item.get('raw_flavor'):
            alias=str(item['raw_flavor']).strip()
            if not any(alias and alias in value for value in contacts):
                from flavor_aliases import learn as learn_flavor
                learn_flavor(alias,product,ai_result.get('confidence',0),catalog,
                    origin='CUSTOMER_CONFIRMED_AI',event=event,confirmation=item.get('_customer_confirmation'))
    for alternate in ai_result.get('alternates') or []:
        if not isinstance(alternate,dict) or not alternate.get('raw_flavor'):continue
        main=[item for item in ai_result.get('items') or [] if isinstance(item,dict)
              and alternate.get('for') in (item.get('flavor'),item.get('raw_flavor'))]
        if len(main)!=1:continue
        matches=[row for row in preview['items'] if row.get('parent_name')==main[0].get('category')
                 and row.get('product_name')==alternate.get('flavor')]
        if len(matches)!=1:continue  # Do not learn an unused backup.
        alias=str(alternate['raw_flavor']).strip()
        if any(alias and alias in value for value in contacts):continue
        product=next((p for p in catalog if p['id']==matches[0]['product_id']),None)
        if product:
            from flavor_aliases import learn as learn_flavor
            learn_flavor(alias,product,alternate.get('confidence',ai_result.get('confidence',0)),catalog,
                origin='CUSTOMER_CONFIRMED_AI',event=event,confirmation=alternate.get('_customer_confirmation'))
