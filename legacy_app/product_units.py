"""Display-only units, with immutable per-order-item snapshots."""
from collections import defaultdict
from sqlalchemy import text, inspect,bindparam

UNITS=('支','颗','盒','瓶','包','套','件')

def migrate(connection):
    connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS product_units (
        product_id INTEGER PRIMARY KEY REFERENCES products(id), unit TEXT NOT NULL,
        actor TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS order_item_units (
        item_id INTEGER PRIMARY KEY REFERENCES order_items(id), unit TEXT NOT NULL)''')
    connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS product_unit_audit (
        id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
        old_unit TEXT, new_unit TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL)''')

def effective(session,product):
    if not inspect(session.connection()).has_table('product_units'):return '支'
    ids=[product.id]
    if product.parent_id:ids.append(product.parent_id)
    for pid in ids:
        value=session.execute(text('SELECT unit FROM product_units WHERE product_id=:id'),dict(id=pid)).scalar()
        if value in UNITS:return value
    return '支'

def set_unit(session,product_id,unit,actor):
    from models import Product
    from datetime import datetime,timezone
    if unit is not None and unit not in UNITS:raise ValueError('计量单位无效')
    product=session.get(Product,int(product_id))
    if not product or product.is_archived:raise ValueError('商品不存在或已归档')
    before=session.execute(text('SELECT unit FROM product_units WHERE product_id=:id'),dict(id=product.id)).scalar()
    if before==unit:return effective(session,product)
    stamp=datetime.now(timezone.utc).isoformat()
    if unit is None:session.execute(text('DELETE FROM product_units WHERE product_id=:id'),dict(id=product.id))
    else:session.execute(text('''INSERT INTO product_units(product_id,unit,actor,updated_at)
        VALUES(:id,:unit,:actor,:at) ON CONFLICT(product_id) DO UPDATE SET
        unit=excluded.unit,actor=excluded.actor,updated_at=excluded.updated_at'''),dict(id=product.id,unit=unit,actor=actor,at=stamp))
    session.execute(text('''INSERT INTO product_unit_audit(product_id,old_unit,new_unit,actor,created_at)
        VALUES(:id,:old,:new,:actor,:at)'''),dict(id=product.id,old=before,new=unit,actor=actor,at=stamp))
    return effective(session,product)

def snapshot(session,item,product):
    session.flush()
    session.execute(text('INSERT INTO order_item_units(item_id,unit) VALUES(:id,:unit)'),
        dict(id=item.id,unit=effective(session,product)))
    session.info.get('unit_snapshot_cache',{}).pop(item.id,None)

def historical(session,item):
    # Old orders predate configurable units and retain their original 支.
    if not hasattr(item,'_sa_instance_state'):return '支'
    prefetch(session,[item])
    return session.info['unit_snapshot_cache'].get(item.id,'支')

def prefetch(session,items):
    identities=[item.id for item in items if hasattr(item,'_sa_instance_state') and item.id is not None]
    if not identities:return
    cache=session.info.setdefault('unit_snapshot_cache',{})
    missing=[identity for identity in identities if identity not in cache]
    if not missing:return
    if not inspect(session.connection()).has_table('order_item_units'):
        cache.update({identity:'支' for identity in missing});return
    rows=dict(session.execute(text('SELECT item_id,unit FROM order_item_units WHERE item_id IN :ids').bindparams(bindparam('ids',expanding=True)),dict(ids=missing)).all())
    cache.update({identity:rows.get(identity,'支') for identity in missing})

def total_label(items):
    totals=defaultdict(int)
    for item in items:totals[item.get('unit') if item.get('unit') in UNITS else '支']+=int(item.get('quantity') or 0)
    return '共'+'、'.join(str(totals[unit])+unit for unit in UNITS if totals[unit])

def stock_line_display(session, product, fifo_details):
    """New movement snapshots are immutable; old rows disclose current-unit fallback."""
    import json
    try:
        details=json.loads(fifo_details) if isinstance(fifo_details,str) else fifo_details
        units={d.get('display_unit') for d in details}
        if details and len(units)==1 and next(iter(units)) in UNITS:
            return dict(unit=next(iter(units)),unit_source='operation_snapshot')
    except (ValueError,TypeError,AttributeError):
        pass
    if product is None:
        return dict(unit=None,unit_source='historical_unknown')
    return dict(unit=effective(session,product),unit_source='current_catalog_not_historical_proof')

def item_unit(item):
    """Order displays use the immutable snapshot, never today's catalog unit."""
    if isinstance(item,dict):
        value=item.get('unit')
        return value if value in UNITS else '支'
    from sqlalchemy.orm import object_session
    session=object_session(item) if hasattr(item,'_sa_instance_state') else None
    return historical(session,item) if session else '支'

def order_total_label(items):
    return total_label([dict(quantity=item.get('quantity',0) if isinstance(item,dict)
                            else getattr(item,'quantity',0),unit=item_unit(item)) for item in items])

def make_router(database,admin_username,security_ready):
    import secrets
    from fastapi import APIRouter,Request,HTTPException
    router=APIRouter()
    def guard(request,mutating=False):
        if not request.session.get('admin_logged_in'):raise HTTPException(401,'请先登录')
        if request.session.get('admin_username')!=admin_username():raise HTTPException(403,'仅最高管理员可修改单位')
        if not security_ready():raise HTTPException(503,'会话安全配置未就绪')
        if mutating and not secrets.compare_digest(request.session.get('unit_csrf','') or 'unset',request.headers.get('X-Unit-CSRF','')):raise HTTPException(403,'请求校验失败')
    @router.get('/api/products/{product_id:int}/unit')
    def get(product_id:int,request:Request):
        from models import Product
        guard(request)
        with database.SessionLocal() as session:
            product=session.get(Product,product_id)
            if not product:raise HTTPException(404,'商品不存在')
            return dict(unit=effective(session,product),allowed=list(UNITS),csrf=request.session.setdefault('unit_csrf',secrets.token_urlsafe(32)))
    @router.post('/api/products/{product_id:int}/unit')
    async def save(product_id:int,request:Request):
        guard(request,True)
        try:body=await request.json()
        except ValueError:raise HTTPException(400,'请求格式无效') from None
        if not isinstance(body,dict) or set(body)!={'unit'}:raise HTTPException(400,'请求字段无效')
        import asyncio
        def commit():
            with database.SessionLocal() as session:
                session.execute(text('BEGIN IMMEDIATE'))
                try:value=set_unit(session,product_id,body['unit'],request.session['admin_username'])
                except ValueError as error:raise HTTPException(400,str(error)) from None
                session.commit();return dict(unit=value)
        return await asyncio.to_thread(commit)
    return router
