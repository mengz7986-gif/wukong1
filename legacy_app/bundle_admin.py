"""Bundle management reuses the existing administrator session and catalog."""
import json
import secrets
from sqlalchemy import inspect,text
from fastapi import APIRouter,HTTPException,Request
from models import Product,User
import bundles


def management_categories(session, category):
    """The existing GLOBAL brand/router identity is the exact standard name.

    These are management representatives only; no inventory or IDs are merged.
    Existing bundles keep their original category/product IDs and snapshots.
    """
    members=session.query(Product).filter_by(is_parent=True,is_archived=False,name=category.name).order_by(Product.id).all()
    active=[p for p in members if p.is_active]
    return (active or members)[0],members


def make_router(database, templates, admin_username, security_ready, catalog):
    router=APIRouter()

    def guard(request,write=False):
        if not request.session.get('admin_logged_in'):
            raise HTTPException(401,'请先登录')
        if request.session.get('admin_username')!=admin_username():
            raise HTTPException(403,'仅管理员可管理福袋')
        if not inspect(database.engine).has_table('bundle_definitions'):
            raise HTTPException(503,'福袋数据库升级尚未完成，普通商品不受影响')
        if write:
            token=request.session.get('bundle_csrf','')
            if not token or not secrets.compare_digest(token,request.headers.get('X-Bundle-CSRF','')):
                raise HTTPException(403,'请求校验失败')
            if not security_ready():raise HTTPException(503,'请先配置安全会话密钥')

    def reserved(session):
        import rule_dictionary
        return [p.name for p in session.query(Product).filter_by(is_active=True,is_archived=False)]+[
            r['alias'] for r in rule_dictionary.read_store()['aliases']+rule_dictionary.builtins()
            if r.get('status')=='active']

    @router.get('/admin/bundles/{category_id}')
    def page(category_id:int,request:Request):
        guard(request)
        with database.SessionLocal() as session:
            category=session.get(Product,category_id)
            if not category or not category.is_parent or category.is_archived:
                raise HTTPException(404,'分类不存在')
            category,_=management_categories(session,category)
            return templates.TemplateResponse('admin/bundles.html',dict(request=request,
                category_id=category.id,category_name=category.name))

    @router.get('/api/bundles/manage/{category_id}')
    def listing(category_id:int,request:Request):
        guard(request);request.session.setdefault('bundle_csrf',secrets.token_urlsafe(32))
        with database.SessionLocal() as session:
            category=session.get(Product,category_id)
            if not category or not category.is_parent or category.is_archived:
                raise HTTPException(404,'分类不存在')
            category,members=management_categories(session,category)
            member_ids=[p.id for p in members]
            all_products=session.query(Product).filter(Product.parent_id.in_(member_ids)).all()
            products={p.id:p for p in all_products if p.parent_id==category.id}
            rows=[]
            for row in session.execute(text('SELECT * FROM bundle_definitions WHERE deleted=0 ORDER BY id')).mappings():
                if row['category_id'] not in member_ids:continue
                item=dict(row,config=json.loads(row['config']))
                owner=session.get(Product,row['category_id'])
                owner_products={p.id:p for p in all_products if p.parent_id==owner.id}
                item['flavors']=[dict(id=p.id,name=p.name,active=p.is_active and not p.is_archived) for p in owner_products.values()]
                item['aliases']=[r[0] for r in session.execute(text("SELECT display_name FROM bundle_names WHERE bundle_id=:id AND kind='alias'"),{'id':row['id']})]
                import bundle_inventory
                try:bundles.validate_config(owner,item['config'],owner_products,inventory_mode=bundle_inventory.view(session,row['id'])['mode']);item['error']=None
                except ValueError as error:item['error']=str(error)
                import bundle_inventory
                item['inventory']=bundle_inventory.view(session,row['id'])
                item['available_portions']=0 if item['error'] else bundle_inventory.capacity(session,item)
                item['inventory_movements']=[]
                if inspect(session.connection()).has_table('bundle_inventory_movements'):
                    item['inventory_movements']=[dict(r) for r in session.execute(text('SELECT id,operation_key,kind,quantity,cost_cents,actor,note,created_at FROM bundle_inventory_movements WHERE bundle_id=:b ORDER BY id DESC LIMIT 50'),{'b':row['id']}).mappings()]
                rows.append(item)
            return dict(csrf=request.session['bundle_csrf'],bundles=rows,category_id=category.id,category_name=category.name,
                flavors=[dict(id=p.id,name=p.name,active=p.is_active and not p.is_archived) for p in products.values()],
                rule=dict(session.execute(text('SELECT * FROM bundle_settings WHERE id=1')).mappings().one()))

    @router.post('/api/bundles/manage/{category_id}')
    async def save(category_id:int,request:Request):
        guard(request,True)
        body=await request.json()
        try:
            with database.SessionLocal() as session:
                session.connection().exec_driver_sql('BEGIN IMMEDIATE')
                category=session.get(Product,category_id)
                products={p.id:p for p in session.query(Product).filter_by(parent_id=category_id)}
                import config
                capacity=int(getattr(config,'UNITS_PER_PACKAGE',0) or 0) or None
                if 'inventory_mode' in body:
                    if body['inventory_mode'] not in ('independent','components'):raise ValueError('库存模式无效')
                    body['config']['show_flavors']=bool(body['config'].get('components') or body['config'].get('pool'))
                id=bundles.save(session,category,body['name'],body['config'],products,
                    aliases=body.get('aliases',[]),bundle_id=body.get('id'),enabled=bool(body.get('enabled')),
                    reserved_names=reserved(session),package_capacity=capacity,
                    inventory_mode=body.get('inventory_mode'))
                if 'inventory_mode' in body:
                    import bundle_inventory
                    if not inspect(session.connection()).has_table('bundle_inventory_settings'):raise ValueError('福袋库存模式升级尚未完成')
                    bundle_inventory.configure(session,id,body['inventory_mode'],body.get('cost_price',0))
                session.commit();return dict(id=id,success=True)
        except (ValueError,KeyError,TypeError) as error:
            raise HTTPException(400,str(error)) from None

    @router.post('/api/bundles/{bundle_id}/inventory')
    async def inventory_adjust(bundle_id:int,request:Request):
        guard(request,True);body=await request.json()
        import bundle_inventory
        try:
            with database.SessionLocal() as session:
                session.connection().exec_driver_sql('BEGIN IMMEDIATE')
                if not inspect(session.connection()).has_table('bundle_inventory_settings'):raise ValueError('福袋库存升级尚未完成')
                if not session.execute(text('SELECT id FROM bundle_definitions WHERE id=:b AND deleted=0'),{'b':bundle_id}).scalar():raise ValueError('福袋不存在')
                key=body.get('operation_id')
                if not isinstance(key,str) or not 16<=len(key)<=128:raise ValueError('请提供库存操作唯一标识')
                key='admin-inventory:'+key
                actor='admin:'+request.session['admin_username']
                if body.get('action')=='in':
                    mid=bundle_inventory.receive(session,bundle_id,body['quantity'],body['cost_price'],key=key,actor=actor,note='后台福袋入库')
                elif body.get('action')=='out':
                    mid=bundle_inventory.consume(session,bundle_id,body['quantity'],key=key,actor=actor,kind='adjust_out',note='后台福袋库存调整')['id']
                else:raise ValueError('请选择入库或调整出库')
                session.commit();return dict(success=True,movement_id=mid,inventory=bundle_inventory.view(session,bundle_id))
        except (ValueError,KeyError,TypeError) as error:raise HTTPException(400,str(error)) from None

    @router.post('/api/bundles/{bundle_id}/status')
    async def status(bundle_id:int,request:Request):
        guard(request,True);body=await request.json()
        try:
            with database.SessionLocal() as session:
                session.connection().exec_driver_sql('BEGIN IMMEDIATE')
                row=session.execute(text('SELECT * FROM bundle_definitions WHERE id=:id AND deleted=0'),{'id':bundle_id}).mappings().first()
                if not row:raise ValueError('福袋不存在')
                action=body.get('action')
                if action not in ('enable','disable','delete'):raise ValueError('操作无效')
                if action=='delete':
                    import bundle_inventory
                    if bundle_inventory.view(session,bundle_id)['remaining']:
                        raise ValueError('仍有独立库存，禁止归档；请先核对并正常调整库存')
                if action=='enable':
                    category=session.get(Product,row['category_id'])
                    products={p.id:p for p in session.query(Product).filter_by(parent_id=row['category_id'])}
                    import config
                    bundles.validate_config(category,json.loads(row['config']),products,
                        int(getattr(config,'UNITS_PER_PACKAGE',0) or 0) or None,
                        inventory_mode=__import__('bundle_inventory').view(session,bundle_id)['mode'])
                    names=[r[0] for r in session.execute(text('SELECT display_name FROM bundle_names WHERE bundle_id=:id'),{'id':bundle_id})]
                    if {bundles.normalized(n) for n in names}.intersection(bundles.normalized(n) for n in reserved(session)):
                        raise ValueError('福袋名称/叫法与当前目录或叫法冲突，不能启用')
                session.execute(text('UPDATE bundle_definitions SET enabled=:enabled,deleted=:deleted,updated_at=CURRENT_TIMESTAMP WHERE id=:id'),dict(enabled=int(action=='enable'),deleted=int(action=='delete'),id=bundle_id))
                if action=='delete':session.execute(text('DELETE FROM bundle_names WHERE bundle_id=:id'),{'id':bundle_id})
                session.commit();return dict(success=True)
        except (ValueError,KeyError,TypeError) as error:raise HTTPException(400,str(error)) from None

    @router.post('/api/settings/bundles')
    async def settings(request:Request):
        guard(request,True);body=await request.json()
        try:
            limit=bundles.positive(body['max_portions'],'每笔限制福袋数量')
            with database.SessionLocal() as session:
                session.execute(text('UPDATE bundle_settings SET max_portions=:limit WHERE id=1'),{'limit':limit})
                session.commit()
            return dict(success=True,max_portions=limit,shipping_mode='free')
        except (ValueError,KeyError,TypeError) as error:raise HTTPException(400,str(error)) from None

    @router.get('/api/settings/bundles')
    def get_settings(request:Request):
        guard(request);request.session.setdefault('bundle_csrf',secrets.token_urlsafe(32))
        with database.SessionLocal() as session:
            return dict(csrf=request.session['bundle_csrf'],rule=dict(session.execute(text('SELECT * FROM bundle_settings WHERE id=1')).mappings().one()))

    @router.post('/api/bundles/{bundle_id}/user-price')
    async def user_price(bundle_id:int,request:Request):
        guard(request,True);body=await request.json()
        try:
            user_id=bundles.positive(body['user_id'],'用户ID')
            with database.SessionLocal() as session:
                if not session.get(User,user_id):raise ValueError('用户不存在')
                if not session.execute(text('SELECT id FROM bundle_definitions WHERE id=:id AND deleted=0'),{'id':bundle_id}).scalar():raise ValueError('福袋不存在')
                if body.get('price') is None:
                    session.execute(text('DELETE FROM bundle_user_prices WHERE bundle_id=:b AND user_id=:u'),{'b':bundle_id,'u':user_id})
                else:
                    value=bundles.price(body['price'])
                    session.execute(text('INSERT INTO bundle_user_prices(bundle_id,user_id,price) VALUES (:b,:u,:p) ON CONFLICT(bundle_id,user_id) DO UPDATE SET price=excluded.price'),{'b':bundle_id,'u':user_id,'p':value})
                session.commit();return dict(success=True)
        except (ValueError,KeyError,TypeError) as error:raise HTTPException(400,str(error)) from None

    return router
