"""彩虹仓规则后台。测试解析只使用本地解析器，不创建订单或调用AI。"""
import re
import secrets
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
import rule_dictionary as dictionary


def logical_brands(categories, products, stored):
    """Use the Parser's exact canonical category/flavor keys, never fuzzy names.

    Warehouse product IDs remain separate; representatives are only edit targets.
    GLOBAL aliases already resolve by canonical category and standard flavor.
    """
    from brand_aliases import scope_matches
    groups = {}
    for category in sorted(categories, key=lambda c: c['id']):
        standard = __import__('logical_categories').category_key(category['name'])
        group = groups.setdefault(standard, dict(
            standard=standard, warehouse_id=category['warehouse_id'],
            category_id=category['id'], warehouse_name=category.get('warehouse_name', ''),
            category_ids=[], warehouses=[], products=[], flavors=[], aliases=[]))
        group['category_ids'].append(category['id'])
        if not any(w['id'] == category['warehouse_id'] for w in group['warehouses']):
            group['warehouses'].append(dict(id=category['warehouse_id'], name=category.get('warehouse_name', '')))
    for product in sorted(products, key=lambda p: p['id']):
        standard = product['parent_name']
        if standard not in groups:
            continue
        group = groups[standard]
        # Only members with an actual category object enter a logical brand.
        if product.get('parent_id') not in group['category_ids']:
            continue
        group['products'].append(product['name'])
        flavor = next((f for f in group['flavors'] if f['standard'] == product['name']), None)
        if flavor is None:
            flavor = dict(product_id=product['id'], product_ids=[], standard=product['name'],
                warehouse_id=product['default_warehouse_id'], category_id=product['parent_id'],
                warehouse_name=product.get('warehouse_name', ''), category=standard, aliases=[])
            group['flavors'].append(flavor)
        flavor['product_ids'].append(product['id'])
    rows = [r for r in dictionary.builtins() + stored['aliases']
            if r.get('status') not in ('quarantined', 'deleted', 'archived')]
    for group in groups.values():
        members = [dict(parent_id=c['id'], default_warehouse_id=c['warehouse_id'], parent_name=c['name'])
                   for c in categories if c['id'] in group['category_ids']]
        group['aliases'] = [r for r in rows if r['type'] == 'MODEL_ALIASES'
            and r['standard'] == group['standard'] and any(scope_matches(r, p) for p in members)]
        for flavor in group['flavors']:
            flavor['aliases'] = [r for r in rows if r['type'] == 'EXPLICIT_FLAVOR'
                and r['category'] == group['standard'] and r['standard'] == flavor['standard']
                and any(scope_matches(r, p) for p in members)]
    return list(groups.values())


def test_parse(text, catalog):
    with dictionary.runtime_snapshot():
        return _test_parse(text, catalog)


def _test_parse(text, catalog):
    from order_parser import OrderParser
    from contact_fields import diagnose_contact
    from bot import OrderBot
    runtime=object.__new__(OrderBot)
    from category_evidence import prepare
    local_text = prepare(text, catalog, dictionary.runtime_store()['aliases'])
    info = OrderParser.extract_order_info(local_text, catalog) or {}
    category_context=runtime._resolve_order_category_context(info,catalog)
    valid, message = OrderParser.validate_order(info)
    items = []
    for name, quantity in runtime._order_catalog_items(info, catalog):
        match = re.fullmatch(r'(?:品类|型号)\((.+?)\)\s*口味\((.+)\)', name)
        category, flavor = match.groups() if match else (None, name)
        item=dict(raw_category=category,raw_flavor=flavor)
        product,error=runtime._resolve_product_for_order(name,catalog,category_context=category_context)
        if product:
            manual=dictionary.manual_flavor(product.get('parent_name'),flavor,
                [p['name'] for p in catalog if p.get('parent_name')==product.get('parent_name')])
            item.update(resolved_category=product.get('parent_name'),resolved_flavor=product['name'],
                product_id=product.get('id'),resolve_reason='人工Alias精确命中' if manual else '真实本地预览目录匹配')
        else:
            item.update(resolved_category=None,resolved_flavor=None,resolve_reason=error or '本地未匹配')
        item.update(quantity=quantity, raw_name=name)
        items.append(item)
    diagnostics = [] if valid else [message]
    for item in items:
        if not item.get('resolved_category') or not item.get('resolved_flavor'):
            diagnostics.append('目录无法唯一匹配：' + item['raw_name'])
    hits = [dict(rule=fact['kind'], evidence=fact) for fact in info.get('quantity_scopes', [])]
    if info.get('contact_diagnostics'):
        hits.append(dict(rule='Contact', evidence=diagnose_contact(info)))
    return dict(items=items, contact={k: info.get(k) for k in ('recipient', 'phone', 'address')},
                rule_hits=hits, dictionary_hits=[dict(category=i.get('resolved_category'),
                product=i.get('resolved_flavor'), reason=i['resolve_reason']) for i in items],
                ai_called=False, ai_policy='测试解析固定本地模式，不调用AI',
                diagnostics=diagnostics, quantity_scopes=info.get('quantity_scopes', []),
                declared_total=info.get('declared_total'), computed_total=info.get('computed_total'),
                scope='解析与目录匹配；不校验库存/履约，不创建订单、不扣库存')


# Public parsing helper is imported by tests; it is not itself a pytest test.
test_parse.__test__ = False


def make_router(templates, admin_username, catalog, security_ready=lambda: True, categories=lambda: [], bundle_catalog=lambda: []):
    router = APIRouter()
    def brand_catalog():
        products=catalog()
        represented={p.get('parent_id') for p in products}
        return products+[dict(id=None,name='',parent_id=c['id'],parent_name=c['name'],
            default_warehouse_id=c['warehouse_id']) for c in categories() if c['id'] not in represented]
    def guard(request, write=False):
        if not request.session.get('admin_logged_in'):
            raise HTTPException(401, '请先登录')
        if write and request.session.get('admin_username') != admin_username():
            raise HTTPException(403, '仅最高管理员可编辑词典')
        if write and not security_ready():
            raise HTTPException(503, '请先配置安全的会话签名密钥')
        if request.method != 'GET':
            expected = request.session.get('rules_csrf') or ''
            if not expected or not secrets.compare_digest(expected, request.headers.get('X-Rules-CSRF', '')):
                raise HTTPException(403, '请求校验失败')

    def response(value):
        return JSONResponse(value, headers={'Cache-Control': 'no-store'})

    @router.get('/admin/rules')
    async def page(request: Request):
        guard(request)
        request.session.setdefault('rules_csrf', secrets.token_urlsafe(32))
        return templates.TemplateResponse(request, 'admin/brands.html', dict(
            rules_csrf=request.session['rules_csrf'],
            rules_can_manage=request.session.get('admin_username') == admin_username() and security_ready()),
            headers={'Cache-Control': 'no-store'})

    @router.get('/api/brands')
    def brands(request: Request):
        guard(request)
        products=catalog();stored=dictionary.read_store()
        live_categories=categories()
        # Older callers provide only a catalog; derive real parent IDs, not fuzzy identities.
        if not live_categories:
            live_categories=list({p['parent_id']:dict(id=p['parent_id'],name=p['parent_name'],
                warehouse_id=p['default_warehouse_id'],warehouse_name=p.get('warehouse_name',''))
                for p in products}.values())
        from bundle_aliases import groups
        manual_pending=[]
        import brand_aliases,flavor_aliases
        for row in stored['aliases']:
            if row.get('readonly') or row.get('status')!='pending' or row.get('type') not in ('MODEL_ALIASES','EXPLICIT_FLAVOR'):continue
            reason=''
            try:
                if row['type']=='MODEL_ALIASES':brand_aliases.validate_approval(row,stored,brand_catalog())
                elif flavor_aliases.plan(dict(row,aliases=row['alias']),products,stored)['conflicts']:reason='同一分类叫法冲突，不能通过'
            except (ValueError,KeyError,TypeError):reason='目录已变化或叫法冲突，不能通过'
            manual_pending.append(dict(id=row['id'],type=row['type'],alias=row['alias'],standard=row['standard'],category=row.get('category'),status='pending',conflict_reason=reason))
        return response(dict(brands=logical_brands(live_categories,products,stored)+groups(bundle_catalog(),stored),candidates=stored.get('brand_candidates',[]),
            manual_pending=manual_pending,
            historical_candidates=[r for r in dictionary.review_candidates() if r['type']=='MODEL_ALIASES']))

    @router.post('/api/brands/aliases')
    async def brand_save(request: Request):
        guard(request, True)
        try:
            from brand_aliases import mutate
            return response(mutate(await request.json(),request.session['admin_username'],brand_catalog()))
        except (ValueError,KeyError,TypeError,OSError) as error:
            raise HTTPException(400,str(error) if isinstance(error,ValueError) else '叫法保存失败') from None

    @router.post('/api/brands/bundle-aliases')
    async def bundle_alias_save(request: Request):
        guard(request, True)
        try:
            from bundle_aliases import mutate
            return response(mutate(await request.json(), request.session['admin_username'], bundle_catalog(), catalog()))
        except (ValueError,KeyError,TypeError,OSError) as error:
            raise HTTPException(400, str(error) if isinstance(error,ValueError) else '福袋叫法保存失败') from None

    @router.post('/api/brands/flavors/{action}')
    async def flavor_action(request: Request, action: str):
        guard(request, True)
        try:
            import flavor_aliases
            body=await request.json()
            if action=='preview': return response(flavor_aliases.preview(body,catalog()))
            if action=='save': return response(flavor_aliases.save(body,request.session['admin_username'],catalog()))
            if action=='mutate': return response(flavor_aliases.mutate(body,request.session['admin_username'],catalog()))
            raise ValueError('操作无效')
        except (ValueError,KeyError,TypeError,OSError) as error:
            raise HTTPException(400,str(error) if isinstance(error,ValueError) else '口味叫法保存失败') from None

    @router.post('/api/brands/aliases/batch/{action}')
    async def brand_batch(request: Request, action: str):
        guard(request,True)
        try:
            from brand_aliases import batch_preview,batch_save,bulk_status
            body=await request.json()
            if action=='preview':return response(batch_preview(body,brand_catalog()))
            if action=='save':return response(batch_save(body,request.session['admin_username'],brand_catalog()))
            if action=='status':return response(bulk_status(body,request.session['admin_username'],brand_catalog()))
            raise ValueError('操作无效')
        except (ValueError,KeyError,TypeError,OSError) as error:
            raise HTTPException(400,str(error) if isinstance(error,ValueError) else '叫法批量操作失败') from None

    @router.post('/api/brands/candidates/batch/{action}')
    async def flavor_candidate_batch(request: Request, action: str):
        guard(request,True)
        try:
            from flavor_aliases import candidate_batch_preview,candidate_batch_review
            body=await request.json()
            if not isinstance(body,dict):raise ValueError('请求格式无效')
            if action=='preview':return response(candidate_batch_preview(body,catalog()))
            if action=='commit':return response(candidate_batch_review(body,request.session['admin_username'],catalog()))
            raise ValueError('操作无效')
        except (ValueError,KeyError,TypeError,OSError) as error:
            raise HTTPException(400,str(error) if isinstance(error,ValueError) else '候选批量操作失败') from None

    @router.post('/api/brands/candidates')
    async def candidate_review(request: Request):
        guard(request, True)
        try:
            from brand_aliases import review
            body=await request.json()
            if not isinstance(body,dict):raise ValueError('请求格式无效')
            return response(review(body,request.session['admin_username'],catalog()))
        except (ValueError,KeyError,TypeError,OSError) as error:
            raise HTTPException(400,str(error) if isinstance(error,ValueError) else '候选审核失败') from None

    @router.get('/api/rules')
    def status(request: Request):
        guard(request)
        try:
            return response(dictionary.overview(dict(request.query_params)))
        except (ValueError, OSError):
            raise HTTPException(503, '词典或Manifest无法读取，请检查文件与备份') from None

    @router.post('/api/rules/aliases')
    async def save(request: Request):
        guard(request, True)
        try:
            body = await request.json()
            # Activating an alias requires a live category and exact live flavor.
            if isinstance(body, dict) and body.get('status') == 'active':
                existing = next((r for r in dictionary.read_store()['aliases'] if r['id'] == body.get('id')), {})
                category = body.get('category', existing.get('category'))
                standard = body.get('standard', existing.get('standard'))
                kind = existing.get('type', 'EXPLICIT_FLAVOR')
                matches = (any(p['parent_name'] == standard for p in catalog()) if kind == 'MODEL_ALIASES'
                           else any(p['parent_name'] == category and p['name'] == standard for p in catalog()))
                if not matches:
                    raise ValueError('启用失败：当前目录没有对应分类/商品')
                if kind == 'MODEL_ALIASES':
                    from deepseek_resolver import enforce_generation
                    alias=body.get('alias',existing.get('alias'))
                    resolved,_=enforce_generation(standard,alias,
                        sorted({p['parent_name'] for p in catalog()}),allow_reject=True)
                    if resolved!=standard:
                        raise ValueError('启用失败：人工型号Alias与代际保护规则冲突')
            return response(dictionary.mutate(body, request.session['admin_username']))
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error) if isinstance(error, ValueError) else '词典写入失败') from None

    @router.post('/api/rules/test')
    async def parse(request: Request):
        guard(request)
        try:
            body = await request.json()
            text = body.get('text') if isinstance(body, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                raise ValueError('请输入1–20000字符测试文本')
            return response(test_parse(text, catalog()))
        except (ValueError, OSError):
            raise HTTPException(400, '测试文本或词典无效') from None

    return router
