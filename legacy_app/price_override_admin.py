"""Session-only, optimistic-preview reset of price overrides, in one DB commit."""
import hashlib,json,secrets
from fastapi import APIRouter,Request,HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text


def inheritance_audit(session, user_id):
    """Read-only classification; equal amounts never establish historical intent.

    Physical flavor rows without trustworthy provenance remain unresolved even
    when they equal a category price. Never migrate or delete them implicitly.
    """
    from models import Product, UserProductPrice, UserCategoryPrice
    categories = {r.parent_id: r.unit_price for r in
                  session.query(UserCategoryPrice).filter_by(user_id=user_id)}
    rows = session.query(UserProductPrice, Product).join(
        Product, Product.id == UserProductPrice.product_id).filter(
        UserProductPrice.user_id == user_id).order_by(UserProductPrice.id).all()
    return {
        'user_id': user_id,
        'category_prices': [{'parent_id': k, 'unit_price': v}
                            for k, v in sorted(categories.items())],
        'flavor_overrides': [dict(
            override_id=r.id, product_id=p.id, parent_id=p.parent_id,
            unit_price=r.unit_price, category_price=categories.get(p.parent_id),
            provenance='UNCONFIRMED', requires_confirmation=True,
            equal_to_category=(p.parent_id in categories and
                               r.unit_price == categories[p.parent_id]),
        ) for r, p in rows],
        'mutated': False,
    }


def preview(session,user_id):
    from models import User,UserProductPrice,UserCategoryPrice
    user=session.get(User,user_id)
    if user is None:raise ValueError('用户不存在')
    rows=[{'id':r.id,'product_id':r.product_id,'unit_price':r.unit_price}
          for r in session.query(UserProductPrice).filter_by(user_id=user_id).order_by(UserProductPrice.id)]
    rows.extend({'id':r.id,'parent_id':r.parent_id,'unit_price':r.unit_price,'scope':'category'}
        for r in session.query(UserCategoryPrice).filter_by(user_id=user_id).order_by(UserCategoryPrice.id))
    token=hashlib.sha256(json.dumps([user_id,user.agent_level,rows],sort_keys=True).encode()).hexdigest()
    return dict(count=len(rows),token=token,agent_level=user.agent_level,rows=rows)


def category_confirmation_preview(session, user_id, parent_id):
    from models import Product, UserProductPrice, User
    from logical_categories import category_ids
    if session.get(User, user_id) is None:raise ValueError('用户不存在')
    parents=category_ids(session,parent_id)
    if not parents:raise ValueError('分类不存在')
    products=session.query(Product).filter(Product.parent_id.in_(parents),
        Product.is_parent==False,Product.is_active==True,Product.is_archived==False).order_by(Product.id).all()
    if not products:raise ValueError('分类没有有效口味')
    ids={p.id for p in products}
    overrides=[dict(product_id=r.product_id,unit_price=r.unit_price)
        for r in session.query(UserProductPrice).filter_by(user_id=user_id).order_by(UserProductPrice.product_id)
        if r.product_id in ids]
    result=dict(user_id=user_id,parent_id=parent_id,parent_ids=sorted(parents),
        products=[dict(id=p.id,name=p.name,warehouse_id=p.default_warehouse_id,parent_id=p.parent_id) for p in products],
        overrides=overrides,price_snapshot=preview(session,user_id))
    result['token']=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    result.pop('price_snapshot')
    return result


def confirm_category_inheritance(session,user_id,parent_id,token,unit_price,convert_ids,actor,price_review_token=None):
    """Explicit selection only. All unselected old flavor rows remain untouched."""
    import math
    from models import UserProductPrice,UserCategoryPrice,PriceOverrideAudit,Product
    from sale_price_review import amount,require_confirmation
    unit_price=amount(unit_price)
    if not isinstance(convert_ids,list) or any(type(i) is not int for i in convert_ids) or len(set(convert_ids))!=len(convert_ids):
        raise ValueError('请逐项确认需要转换的口味')
    current=category_confirmation_preview(session,user_id,parent_id)
    if not isinstance(token,str) or not secrets.compare_digest(token,current['token']):
        raise ValueError('价格或商品目录已变化，请重新预览')
    existing={r['product_id'] for r in current['overrides']}
    if not set(convert_ids)<=existing:raise ValueError('转换范围超出已预览口味')
    before=preview(session,user_id)
    retained={r['product_id']:r['unit_price'] for r in current['overrides'] if r['product_id'] not in convert_ids}
    require_confirmation(session,[(session.get(Product,p['id']),retained.get(p['id'],unit_price)) for p in current['products']],
        dict(action='explicit_category_inheritance',user_id=user_id,parent_id=parent_id,token=token,
             convert_ids=sorted(convert_ids),requested_price=unit_price),price_review_token)
    for pid in current['parent_ids']:
        row=session.query(UserCategoryPrice).filter_by(user_id=user_id,parent_id=pid).first()
        if row is None:session.add(UserCategoryPrice(user_id=user_id,parent_id=pid,unit_price=unit_price))
        else:row.unit_price=unit_price
    if convert_ids:
        session.query(UserProductPrice).filter(UserProductPrice.user_id==user_id,
            UserProductPrice.product_id.in_(convert_ids)).delete(synchronize_session=False)
    session.add(PriceOverrideAudit(user_id=user_id,actor=actor,before_json=json.dumps(dict(
        action='explicit_category_inheritance_confirmation',before=before['rows'],
        parent_ids=current['parent_ids'],product_ids=[p['id'] for p in current['products']],
        converted_product_ids=sorted(convert_ids),retained_product_ids=sorted(existing-set(convert_ids)),
        new_category_price=unit_price),sort_keys=True,ensure_ascii=False)))
    return dict(success=True,converted_count=len(convert_ids),retained_count=len(existing-set(convert_ids)))


def reset(session,user_id,token,actor,price_review_token=None):
    from models import UserProductPrice,UserCategoryPrice,PriceOverrideAudit,Product,User
    current=preview(session,user_id)
    if not isinstance(token,str) or not secrets.compare_digest(token,current['token']):
        raise ValueError('用户等级或专属价已变化，请重新预览')
    if not current['count']:return dict(deleted=0)
    product_ids={r['product_id'] for r in current['rows'] if 'product_id' in r}
    parent_ids={r['parent_id'] for r in current['rows'] if 'parent_id' in r}
    products=session.query(Product).filter((Product.id.in_(product_ids)) | (Product.parent_id.in_(parent_ids))).all()
    user=session.get(User,user_id)
    from sale_price_review import require_confirmation
    require_confirmation(session,[(p,p.get_price_for_agent_level(user.agent_level)) for p in products],
        dict(action='reset_to_grade_price',user_id=user_id,token=token),price_review_token)
    session.add(PriceOverrideAudit(user_id=user_id,actor=actor,before_json=json.dumps(current['rows'],sort_keys=True)))
    deleted=session.query(UserProductPrice).filter_by(user_id=user_id).delete(synchronize_session=False)
    deleted+=session.query(UserCategoryPrice).filter_by(user_id=user_id).delete(synchronize_session=False)
    return dict(deleted=deleted)

def category_batch_preview(session,user_id,changes):
    """Normal category edits preserve every flavor override, including legacy."""
    import math
    from decimal import Decimal
    from models import User,Product,UserCategoryPrice,UserProductPrice
    if not isinstance(changes,list) or not 1<=len(changes)<=100:raise ValueError('请选择1至100个分类')
    user=session.get(User,user_id)
    if user is None:raise ValueError('用户不存在')
    seen=set();rows=[]
    if any(not isinstance(x,dict) or set(x) not in ({'parent_id','unit_price'},{'parent_id','unit_price','follow_all'})
           or type(x['parent_id']) is not int or ('follow_all' in x and type(x['follow_all']) is not bool) for x in changes):raise ValueError('分类字段无效')
    for change in sorted(changes,key=lambda x:x['parent_id']):
        value=change['unit_price']
        if value is not None and (type(value) not in (float,int) or not math.isfinite(value) or value<0 or value>1000000 or Decimal(str(value)).as_tuple().exponent < -2):raise ValueError('分类价格须为0至1000000且最多两位小数，删除请用空值')
        current=category_confirmation_preview(session,user_id,change['parent_id'])
        parents=set(current['parent_ids'])
        if seen & parents:raise ValueError('不能重复修改同一Canonical分类')
        seen.update(parents)
        category_prices={r.parent_id:r.unit_price for r in session.query(UserCategoryPrice).filter(UserCategoryPrice.user_id==user_id,UserCategoryPrice.parent_id.in_(parents))}
        overrides={r['product_id']:r['unit_price'] for r in current['overrides']}
        impact=[]
        for product in current['products']:
            p=session.get(Product,product['id']);level=p.get_price_for_agent_level(user.agent_level)
            from dynamic_prices import custom
            before=custom(p,overrides[p.id]) if p.id in overrides else custom(p,category_prices[p.parent_id]) if p.parent_id in category_prices else level
            retained=p.id in overrides and not change.get('follow_all',False)
            after=custom(p,overrides[p.id]) if retained else level if value is None else custom(p,value)
            from sale_price_review import review
            impact.append(dict(product_id=p.id,parent_id=p.parent_id,warehouse_id=p.default_warehouse_id,before=before,after=after,retained_override=retained,
                               sale_review=review(session,p,after)))
        rows.append(dict(parent_id=change['parent_id'],parent_ids=sorted(parents),unit_price=value,
            before_category_prices=[dict(parent_id=k,unit_price=category_prices.get(k)) for k in sorted(parents)],
            follow_all=change.get('follow_all',False),
            product_count=len(impact),retained_override_ids=[] if change.get('follow_all') else sorted(overrides),
            retained_override_count=0 if change.get('follow_all') else len(overrides),
            cleared_override_ids=sorted(overrides) if change.get('follow_all') else [],
            affected_count=sum(r['before']!=r['after'] for r in impact),products=impact,catalog_token=current['token']))
    result=dict(user_id=user_id,categories=rows,price_token=preview(session,user_id)['token'])
    result['token']=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return result

def save_category_batch(session,user_id,changes,token,actor):
    from models import UserCategoryPrice,UserProductPrice,PriceOverrideAudit
    current=category_batch_preview(session,user_id,changes)
    if not isinstance(token,str) or not secrets.compare_digest(token,current['token']):raise ValueError('价格、等级或目录已变化，请重新预览')
    for category in current['categories']:
        if category['follow_all'] and category['cleared_override_ids']:
            session.query(UserProductPrice).filter(UserProductPrice.user_id==user_id,
                UserProductPrice.product_id.in_(category['cleared_override_ids'])).delete(synchronize_session=False)
        for parent in category['parent_ids']:
            row=session.query(UserCategoryPrice).filter_by(user_id=user_id,parent_id=parent).first()
            if category['unit_price'] is None:
                if row is not None:session.delete(row)
            elif row is None:session.add(UserCategoryPrice(user_id=user_id,parent_id=parent,unit_price=category['unit_price']))
            else:row.unit_price=category['unit_price']
    session.add(PriceOverrideAudit(user_id=user_id,actor=actor,before_json=json.dumps(dict(
        action='category_price_batch',categories=current['categories'],historical_orders_repriced=False,
        flavor_overrides_modified=any(c['follow_all'] for c in current['categories'])),ensure_ascii=False,sort_keys=True)))
    return dict(success=True,category_count=len(current['categories']),
                retained_flavor_overrides=not any(c['follow_all'] for c in current['categories']))

def flavor_delete_preview(session,user_id,product_id):
    from models import Product,User,UserProductPrice,UserCategoryPrice
    product=session.get(Product,product_id);user=session.get(User,user_id)
    if not product or product.is_parent or not user:raise ValueError('用户或口味不存在')
    row=session.query(UserProductPrice).filter_by(user_id=user_id,product_id=product_id).first()
    if row is None:raise ValueError('该口味没有单独专属价')
    category=session.query(UserCategoryPrice).filter_by(user_id=user_id,parent_id=product.parent_id).first()
    from dynamic_prices import custom
    result=dict(user_id=user_id,product_id=product_id,name=product.name,warehouse_id=product.default_warehouse_id,
        before=custom(product,row.unit_price),before_base=row.unit_price,
        after=custom(product,category.unit_price) if category else product.get_price_for_agent_level(user.agent_level),
        price_token=preview(session,user_id)['token'])
    result['token']=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return result


def make_router(database,admin_username,security_ready):
    router=APIRouter()
    def guard(request,mutating=False):
        if not request.session.get('admin_logged_in'):raise HTTPException(401,'请先登录')
        if request.session.get('admin_username')!=admin_username():raise HTTPException(403,'仅最高管理员可恢复价格')
        if not security_ready():raise HTTPException(503,'会话安全配置未就绪')
        if mutating:
            expected=request.session.get('price_reset_csrf','')
            if not expected or not secrets.compare_digest(expected,request.headers.get('X-Price-CSRF','')):
                raise HTTPException(403,'请求校验失败，请刷新')
    @router.get('/api/users/{user_id:int}/prices/reset-preview')
    def price_preview(user_id:int,request:Request):
        guard(request)
        with database.SessionLocal() as session:
            try:result=preview(session,user_id)
            except ValueError as error:raise HTTPException(404,str(error)) from None
        # Only expose confirmation metadata, not an unnecessary second copy of prices.
        result.pop('rows')
        result['csrf']=request.session.setdefault('price_reset_csrf',secrets.token_urlsafe(32))
        return JSONResponse(result,headers={'Cache-Control':'no-store','Pragma':'no-cache'})
    @router.post('/api/users/{user_id:int}/prices/reset')
    async def price_reset(user_id:int,request:Request):
        guard(request,True)
        try:body=await request.json()
        except ValueError:raise HTTPException(400,'请求格式无效') from None
        if not isinstance(body,dict) or not {'token','confirm'}<=set(body)<= {'token','confirm','price_review_token'} or body.get('confirm') is not True:
            raise HTTPException(400,'请预览并明确确认恢复等级价')
        import asyncio
        def commit():
            with database.SessionLocal() as session:
                if session.get_bind().dialect.name=='sqlite':session.execute(text('BEGIN IMMEDIATE'))
                try:result=reset(session,user_id,body.get('token'),request.session['admin_username'],body.get('price_review_token'))
                except ValueError as error:raise HTTPException(409,str(error)) from None
                session.commit();return result
        return await asyncio.to_thread(commit)
    @router.get('/api/users/{user_id:int}/prices/category-inheritance/{parent_id:int}')
    def inheritance_preview(user_id:int,parent_id:int,request:Request):
        guard(request)
        with database.SessionLocal() as session:
            try:result=category_confirmation_preview(session,user_id,parent_id)
            except ValueError as error:raise HTTPException(400,str(error)) from None
        result['csrf']=request.session.setdefault('price_reset_csrf',secrets.token_urlsafe(32))
        return JSONResponse(result,headers={'Cache-Control':'no-store','Pragma':'no-cache'})
    @router.post('/api/users/{user_id:int}/prices/category-inheritance/{parent_id:int}')
    async def inheritance_confirm(user_id:int,parent_id:int,request:Request):
        guard(request,True)
        try:body=await request.json()
        except ValueError:raise HTTPException(400,'请求格式无效') from None
        if not isinstance(body,dict) or not {'token','confirm','unit_price','convert_ids'}<=set(body)<= {'token','confirm','unit_price','convert_ids','price_review_token'} or body['confirm'] is not True:
            raise HTTPException(400,'请预览并逐项确认分类继承')
        import asyncio
        def commit():
            with database.SessionLocal() as session:
                if session.get_bind().dialect.name=='sqlite':session.execute(text('BEGIN IMMEDIATE'))
                try:result=confirm_category_inheritance(session,user_id,parent_id,body['token'],
                    body['unit_price'],body['convert_ids'],request.session['admin_username'],body.get('price_review_token'))
                except ValueError as error:raise HTTPException(409,str(error)) from None
                session.commit();return result
        return await asyncio.to_thread(commit)
    @router.post('/api/users/{user_id:int}/prices/categories/preview')
    async def batch_category_preview(user_id:int,request:Request):
        guard(request,True)
        try:body=await request.json()
        except ValueError:raise HTTPException(400,'请求格式无效') from None
        if not isinstance(body,dict) or set(body)!={'categories'}:raise HTTPException(400,'分类预览字段无效')
        import asyncio
        def read():
            with database.SessionLocal() as session:
                try:return category_batch_preview(session,user_id,body['categories'])
                except ValueError as error:raise HTTPException(400,str(error)) from None
        return JSONResponse(await asyncio.to_thread(read),headers={'Cache-Control':'no-store'})
    @router.post('/api/users/{user_id:int}/prices/categories/save')
    async def batch_category_save(user_id:int,request:Request):
        guard(request,True)
        try:body=await request.json()
        except ValueError:raise HTTPException(400,'请求格式无效') from None
        if not isinstance(body,dict) or set(body)!={'categories','token','confirm'} or body['confirm'] is not True:raise HTTPException(400,'请预览影响并明确确认')
        import asyncio
        def commit():
            with database.SessionLocal() as session:
                if session.get_bind().dialect.name=='sqlite':session.execute(text('BEGIN IMMEDIATE'))
                try:result=save_category_batch(session,user_id,body['categories'],body['token'],request.session['admin_username'])
                except ValueError as error:raise HTTPException(409,str(error)) from None
                session.commit();return result
        return await asyncio.to_thread(commit)
    @router.get('/api/users/{user_id:int}/prices/flavor/{product_id:int}')
    def flavor_clear_preview(user_id:int,product_id:int,request:Request):
        guard(request)
        with database.SessionLocal() as session:
            try:result=flavor_delete_preview(session,user_id,product_id)
            except ValueError as error:raise HTTPException(400,str(error)) from None
        result['csrf']=request.session.setdefault('price_reset_csrf',secrets.token_urlsafe(32))
        return JSONResponse(result,headers={'Cache-Control':'no-store'})
    @router.post('/api/users/{user_id:int}/prices/flavor/{product_id:int}')
    async def flavor_clear(user_id:int,product_id:int,request:Request):
        guard(request,True)
        try:body=await request.json()
        except ValueError:raise HTTPException(400,'请求格式无效') from None
        if not isinstance(body,dict) or not {'token','confirm'}<=set(body)<= {'token','confirm','price_review_token'} or body['confirm'] is not True:raise HTTPException(400,'请确认删除单独口味价')
        import asyncio
        def commit():
            from models import UserProductPrice,PriceOverrideAudit,Product
            with database.SessionLocal() as session:
                if session.get_bind().dialect.name=='sqlite':session.execute(text('BEGIN IMMEDIATE'))
                try:current=flavor_delete_preview(session,user_id,product_id)
                except ValueError as error:raise HTTPException(409,str(error)) from None
                if not isinstance(body['token'],str) or not secrets.compare_digest(body['token'],current['token']):raise HTTPException(409,'价格已变化，请重新预览')
                from sale_price_review import require_confirmation
                require_confirmation(session,[(session.get(Product,product_id),current['after'])],
                    dict(action='flavor_override_delete',user_id=user_id,product_id=product_id,token=body['token']),body.get('price_review_token'))
                session.query(UserProductPrice).filter_by(user_id=user_id,product_id=product_id).delete()
                session.add(PriceOverrideAudit(user_id=user_id,actor=request.session['admin_username'],before_json=json.dumps(dict(action='flavor_override_delete',**current),ensure_ascii=False,sort_keys=True)))
                session.commit();return dict(success=True,product_id=product_id)
        return await asyncio.to_thread(commit)
    return router
