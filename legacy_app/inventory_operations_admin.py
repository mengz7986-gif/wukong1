"""Batch-operation API attached to the existing inventory page."""
import asyncio
import secrets
from fastapi import APIRouter, Request, HTTPException
from sqlalchemy import text, inspect
from sqlalchemy.orm import Session
import inventory_operations as operations


def make_router(database, security_ready, admin_username=None):
    router=APIRouter()

    def guard(request,write=False):
        if not request.session.get('admin_logged_in'):
            raise HTTPException(401,'请先登录')
        if admin_username and request.session.get('admin_username') != admin_username():
            raise HTTPException(403,'仅管理员可管理库存')
        if request.method != 'GET':
            expected=request.session.get('stock_operations_csrf','')
            if not expected or not secrets.compare_digest(expected,request.headers.get('X-Stock-CSRF','')):
                raise HTTPException(403,'请求校验失败')
        if write and not security_ready():
            raise HTTPException(503,'当前安全门禁不允许库存写操作')

    def ready():
        if not inspect(database.engine).has_table('stock_operations'):
            raise HTTPException(503,'出入库批次Migration尚未执行，禁止写入')

    @router.get('/api/stock/operations/access')
    def access(request:Request):
        guard(request)
        token=request.session.setdefault('stock_operations_csrf',secrets.token_urlsafe(32))
        return dict(csrf=token,can_manage=security_ready())

    @router.get('/api/stock/operations')
    def listing(request:Request,warehouse_id:int=None,category_id:int=None,logical_category_id:int=None,operation_type:str=None,
                start_date:str=None,end_date:str=None,q:str=None):
        guard(request); ready()
        with database.engine.connect() as c:
            sql='SELECT * FROM stock_operations'
            params={}
            clauses=[]
            if warehouse_id is not None:
                clauses.append('warehouse_id=:warehouse'); params['warehouse']=warehouse_id
            if category_id is not None:
                clauses.append('category_id=:category'); params['category']=category_id
            if logical_category_id is not None:
                with database.SessionLocal() as session:
                    ids=__import__('logical_categories').category_ids(session,logical_category_id)
                names=['logical_category_'+str(i) for i in range(len(ids))]
                clauses.append('category_id IN ('+','.join(':'+name for name in names)+')')
                params.update(dict(zip(names,ids)))
            if operation_type:
                if operation_type not in ('inbound','outbound'): raise HTTPException(400,'类型无效')
                clauses.append('operation_type=:kind'); params['kind']=operation_type
            from business_time_range import bounds
            try:
                first,last=bounds(start_date,end_date)
                if first is not None:
                    params['start']=first.isoformat(timespec='seconds');clauses.append("replace(created_at,' ','T')>=:start")
                if last is not None:
                    params['end']=last.isoformat(timespec='seconds');clauses.append("replace(created_at,' ','T')<:end")
            except ValueError as exc: raise HTTPException(400,str(exc)) from None
            if q:
                params['q']='%'+q+'%'
                clauses.append("(id LIKE :q OR note LIKE :q OR EXISTS (SELECT 1 FROM stock_operation_lines l JOIN products p ON p.id=l.product_id WHERE l.operation_id=stock_operations.id AND p.name LIKE :q))")
            if clauses: sql+=' WHERE '+' AND '.join(clauses)
            sql+=' ORDER BY created_at DESC,id DESC LIMIT 300'
            rows=[dict(r) for r in c.execute(text(sql),params).mappings()]
            for row in rows:
                row['lines']=[dict(r) for r in c.execute(text('SELECT * FROM stock_operation_lines WHERE operation_id=:id'),{'id':row['id']}).mappings()]
            # Read-time reporting reflects proven supplementation without
            # rewriting original operation amounts or their audit provenance.
            import json
            from inventory_costs import resolve_details
            with Session(bind=c) as session:
                from models import Warehouse, Product
                for row in rows:
                    warehouse=session.get(Warehouse,row['warehouse_id']);category=session.get(Product,row['category_id'])
                    row['warehouse_name']=warehouse.name if warehouse else '已删除仓库'
                    row['category_name']=category.name if category else '已删除分类'
                    total=0.0
                    for line in row['lines']:
                        product=session.get(Product,line['product_id'])
                        line['product_name']=product.name if product else '已删除商品'
                        from product_units import stock_line_display
                        line.update(stock_line_display(session,product,line['fifo_details']))
                        try:
                            details=resolve_details(session,json.loads(line['fifo_details']))
                            if sum(d['qty'] for d in details)!=line['quantity']:
                                raise ValueError('成本来源数量不匹配')
                            amount=sum(d['qty']*d['unit_cost'] for d in details)
                            line['unit_cost']=amount/line['quantity']
                            if total is not None: total+=amount
                        except (ValueError,TypeError,KeyError):
                            line['unit_cost']=None;total=None
                    row['total_cost']=None if total is None else round(total,2)
                    row['cost_status']='COST_INCOMPLETE' if total is None else 'COST_COMPLETE'
            return rows

    @router.get('/api/stock/operations/valuation')
    def valuation(request:Request):
        guard(request)
        from inventory_costs import baseline, VERIFIED
        from models import InventoryBatch, Product
        from inventory_reference import reference_amount, price_state
        with database.SessionLocal() as session:
            rows=baseline(session)
            for row in rows:
                product=session.get(Product,row['product_id'])
                row.update(reference_amount(max(0,row['physical_qty']),
                    product.cost_price if product else None, product.reference_price_state if product else 'UNSET'))
                row['reference_price_state']=price_state(product.cost_price,product.reference_price_state) if product else 'UNSET'
                import math
                price=product.cost_price if product else None
                row['reference_unit_price']=str(price) if price is not None and math.isfinite(price) and price>=0 else None
                row['verified_value']=round(sum(float(b.unit_cost)*int(b.remain_qty or 0)
                    for b in session.query(InventoryBatch).filter_by(product_id=row['product_id'],warehouse_id=row['warehouse_id'])
                    if b.cost_source in VERIFIED),2)
                row['cost_incomplete']=not row['conserved'] or row['verified_cost_qty']!=row['physical_qty']
            return rows

    async def values(request):
        body=await request.json()
        if not isinstance(body,dict): raise HTTPException(400,'请求格式无效')
        try:
            warehouse=int(body.get('warehouse_id',0)); category=int(body.get('category_id',0))
        except (ValueError,TypeError): raise HTTPException(400,'请选择仓库和分类') from None
        raw=body.get('text')
        if not isinstance(raw,str) or not 1<=len(raw)<=20000:
            raise HTTPException(400,'请输入1–20000字符的出入库明细')
        note=body.get('note','')
        if not isinstance(note,str) or len(note)>500: raise HTTPException(400,'备注过长或无效')
        return warehouse,category,raw,body.get('operation_type'),note,body.get('actual_unit_cost')

    @router.post('/api/stock/operations/preview')
    async def preview(request:Request):
        guard(request)
        warehouse,category,raw,kind,_,cost=await values(request)
        try:
            return await asyncio.to_thread(operations.preview,database,warehouse,category,raw,kind,cost)
        except ValueError as exc: raise HTTPException(400,str(exc)) from None

    @router.get('/api/stock/operations/historical-costs')
    def historical_costs(request:Request):
        guard(request)
        from inventory_costs import listing
        from models import Product
        with database.SessionLocal() as session:
            from product_units import stock_line_display
            rows=listing(session)
            for row in rows:
                row.update(stock_line_display(session,session.get(Product,row['product_id']),None))
            return rows

    @router.post('/api/stock/operations/historical-costs')
    async def supplement_costs(request:Request):
        guard(request,True); ready()
        body=await request.json()
        if not isinstance(body,dict): raise HTTPException(400,'请求格式无效')
        from inventory_costs import supplement
        try:
            ids=[int(body.get(k,0)) for k in ('warehouse_id','category_id','product_id')]
            layer_id=body.get('layer_id')
            if layer_id is not None:
                if isinstance(layer_id,bool) or not isinstance(layer_id,int) or layer_id<=0:
                    raise ValueError('请选择有效成本来源层')
            return await asyncio.to_thread(supplement,database,*ids,body.get('batches'),
                request.session.get('admin_username','admin'),body.get('evidence'),layer_id)
        except (ValueError,TypeError) as exc: raise HTTPException(400,str(exc)) from None

    @router.post('/api/stock/operations')
    async def execute(request:Request):
        guard(request,True); ready()
        warehouse,category,raw,kind,note,cost=await values(request)
        try:
            return await asyncio.to_thread(operations.execute,database,warehouse,category,raw,kind,
                                           request.session.get('admin_username','admin'),note,actual_unit_cost=cost)
        except ValueError as exc: raise HTTPException(400,str(exc)) from None

    @router.post('/api/stock/operations/{operation_id}/cancel')
    async def cancel(request:Request,operation_id:str):
        guard(request,True); ready()
        body=await request.json()
        if not isinstance(body,dict) or not isinstance(body.get('reason'),str) or not 1<=len(body['reason'])<=500:
            raise HTTPException(400,'请输入有效取消原因')
        try:
            return await asyncio.to_thread(operations.cancel,database,operation_id,
                request.session.get('admin_username','admin'),body['reason'])
        except ValueError as exc: raise HTTPException(400,str(exc)) from None

    return router
