"""Catalog restore and atomic batch creation; existing inbound cost rules only."""
import hashlib
import json
import math
import re
import uuid
from datetime import datetime
from sqlalchemy import text
from models import Product, Warehouse, WarehouseStock, InventoryBatch


def reusable(session, parent_id, name):
    rows=session.query(Product).filter_by(parent_id=parent_id,name=name,is_parent=False).order_by(Product.id).all()
    live=[p for p in rows if not p.is_archived]
    if len(live)>1:raise ValueError('存在多个有效同名口味，请先处理目录冲突')
    if live:return live[0],'existing'
    if len(rows)>1:raise ValueError('存在多个归档同名口味，无法安全确定恢复对象')
    if not rows:return None,'create'
    p=rows[0]
    if any(int(s.quantity or 0)!=0 for s in session.query(WarehouseStock).filter_by(product_id=p.id)):
        raise ValueError('归档口味仍有关联库存，不能自动恢复，请先核对库存')
    if any(int(b.remain_qty or 0)!=0 for b in session.query(InventoryBatch).filter_by(product_id=p.id)):
        raise ValueError('归档口味仍有未消耗进货批次，不能自动恢复')
    layers=session.execute(text('SELECT COUNT(*) FROM inventory_unknown_cost WHERE product_id=:p AND remain_qty<>0'),{'p':p.id}).scalar() if session.bind.dialect.has_table(session.connection(),'inventory_unknown_cost') else 0
    if layers:raise ValueError('归档口味仍有历史库存层，不能自动恢复')
    return p,'restore'


def parse_text(raw,parent_id,warehouse_id):
    rows=[]
    for number,line in enumerate(str(raw or '').splitlines(),1):
        if not line.strip():continue
        match=re.fullmatch(r'(.+?)\s+([0-9]+)\s*',line.strip())
        if not match:raise ValueError(f'第{number}行请使用「口味 数量」，数量必须为非负整数')
        name,quantity=match.groups()
        rows.append(dict(line_no=number,product_name=name.strip(),initial_stock=int(quantity),parent_id=parent_id,warehouse_id=warehouse_id))
    if not rows:raise ValueError('请输入一行一个「口味 数量」')
    return rows


def plan(session,items):
    rows=[];errors=[];seen=set()
    for index,item in enumerate(items,1):
        number=item.get('line_no') or index
        row=dict(line_no=number,product_name=str(item.get('product_name') or '').strip(),quantity=item.get('initial_stock',0),errors=[])
        try:
            if not row['product_name'] or '\n' in row['product_name'] or '\r' in row['product_name']:raise ValueError('口味名称不能为空或包含换行')
            qty=row['quantity']
            if isinstance(qty,bool) or not isinstance(qty,int) or qty<0:raise ValueError('数量必须为非负整数')
            warehouse=session.get(Warehouse,item['warehouse_id']) if item.get('warehouse_id') else session.query(Warehouse).filter_by(name=item.get('warehouse_name'),is_active=True).first()
            if not warehouse or not warehouse.is_active:raise ValueError('请选择有效仓库')
            parent=session.get(Product,item['parent_id']) if item.get('parent_id') else session.query(Product).filter_by(name=item.get('category_name') or '默认分类',is_parent=True,default_warehouse_id=warehouse.id,is_archived=False).first()
            if not parent or not parent.is_parent or parent.is_archived or not parent.is_active:raise ValueError('请选择有效分类')
            if parent.default_warehouse_id!=warehouse.id:raise ValueError('分类不属于所选仓库')
            key=(parent.id,row['product_name'])
            if key in seen:raise ValueError('本批口味重复，请合并后再提交')
            seen.add(key)
            product,action=reusable(session,parent.id,row['product_name'])
            if product and product.default_warehouse_id!=warehouse.id:raise ValueError('原商品仓库与分类不一致，不能自动恢复')
            source=product if product else parent
            prices={f:float(getattr(source,f) or 0) for f in ('cost_price','level1_agent_price','level2_agent_price','regular_price')}
            state=source.reference_price_state
            if action!='existing':
                if not all(math.isfinite(v) and v>=0 for v in prices.values()):raise ValueError('当前商品价格无效')
                if not prices['level1_agent_price']<=prices['level2_agent_price']<=prices['regular_price']:raise ValueError('当前销售等级价格层级不正确')
                # New dynamic flavors use the existing parent+offset property.
                import dynamic_prices
                extra=2 if not product and parent.name=='高维' and row['product_name'] in dynamic_prices.SPECIAL_FLAVORS else 0
                from inventory_reference import reference_amount
                if qty and reference_amount(1,prices['cost_price']+extra,state)['reference_incomplete']:raise ValueError('缺少有效成本或成本待确认，禁止产生库存')
            else:extra=0
            row.update(action=action,product_id=product.id if product else None,parent_id=parent.id,warehouse_id=warehouse.id,
                category_name=parent.name,warehouse_name=warehouse.name,prices=prices,reference_price_state=state,
                low_stock_threshold=item.get('low_stock_threshold') if item.get('low_stock_threshold') is not None else source.low_stock_threshold,
                description=item.get('description') or None,
                current_state={'existing':'已存在','restore':'已归档','create':'不存在'}[action],
                execution_result={'existing':'跳过，不重复创建或增加库存','restore':'将恢复/重新启用（不恢复历史库存）','create':'将新增'}[action],
                incoming_quantity=0 if action=='existing' else qty,
                unit_cost=prices['cost_price']+extra if qty and action!='existing' else None)
            warnings=[]
            if action!='existing':
                from sale_price_review import amount
                sale_values=[amount(prices[k]) for k in ('level1_agent_price','level2_agent_price','regular_price')]
                if 0 in sale_values:warnings.append('当前售价为0元，是否确认保存？')
                if row['unit_cost'] is None:warnings.append('当前商品成本尚未确认，无法准确计算预计利润。')
                elif min(sale_values)<row['unit_cost']:warnings.append('当前售价低于本次入库成本，预计产生亏损，是否确认保存？')
            row['price_warnings']=warnings
        except (ValueError,TypeError) as exc:
            row['errors']=[str(exc)];errors.append(f'第{number}行：{exc}')
        rows.append(row)
    if not rows:errors.append('请输入批量商品数据')
    token=hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
    return dict(items=rows,errors=errors,valid=not errors,token=token)


def initial_stock(database,session,product,quantity,actor='admin'):
    """Same inbound_snapshot rules as arrival inventory; never resurrect old lots."""
    if not quantity:
        if not session.query(WarehouseStock).filter_by(product_id=product.id,warehouse_id=product.default_warehouse_id).first():
            session.add(WarehouseStock(product_id=product.id,warehouse_id=product.default_warehouse_id,quantity=0))
        database.refresh_product_stock_status(session,product.id)
        return None
    operation='STK-'+uuid.uuid4().hex
    result=database.inbound_stock(product.id,product.default_warehouse_id,quantity,unit_cost=None,
        batch_no=operation,note='添加口味初始入库；来源：商品成本',update_product_cost=False,session=session)
    if not result.get('success'):raise ValueError(result.get('error','初始入库失败'))
    cost=result['unit_cost']
    session.execute(text('''INSERT INTO stock_operations
      (id,operation_type,warehouse_id,category_id,status,actor,note,created_at,total_quantity,total_cost,cost_status)
      VALUES (:id,'inbound',:w,:c,'completed',:actor,'添加口味初始入库',:at,:q,:cost,'COST_COMPLETE')'''),
      dict(id=operation,w=product.default_warehouse_id,c=product.parent_id,actor=actor,at=datetime.utcnow().isoformat(),q=quantity,cost=round(quantity*cost,2)))
    details=json.dumps([dict(batch_id=result['batch_id'],qty=quantity,unit_cost=cost,cost_source='VERIFIED_REAL_COST',cost_origin='商品成本')])
    session.execute(text('''INSERT INTO stock_operation_lines
      (operation_id,product_id,quantity,before_quantity,after_quantity,unit_cost,before_reference_cost,after_reference_cost,fifo_details)
      VALUES (:op,:p,:q,:before,:after,:cost,:ref,:ref,:details)'''),dict(op=operation,p=product.id,q=quantity,
      before=result['before'],after=result['after'],cost=cost,ref=product.cost_price,details=details))
    return operation


def execute(database,session,items,next_serial,token=None,actor='admin'):
    if session.bind.dialect.name=='sqlite':session.execute(text('BEGIN IMMEDIATE'))
    preview=plan(session,items)
    if not preview['valid']:raise ValueError('；'.join(preview['errors']))
    if not token or token!=preview['token']:raise ValueError('请先明确确认预览；目录、价格或库存状态变化需重新预览校验')
    results=[]
    for row in preview['items']:
        if row['action']=='existing':results.append(row);continue
        if row['action']=='restore':
            product=session.get(Product,row['product_id'])
            product.is_archived=False;product.is_active=True;product.auto_unlisted=False
        else:
            product=Product(name=row['product_name'],parent_id=row['parent_id'],default_warehouse_id=row['warehouse_id'],
                reference_price_state=row['reference_price_state'],description=row['description'],
                low_stock_threshold=row['low_stock_threshold'],product_serial=next_serial(session,False),**row['prices'])
            session.add(product)
        session.flush()
        operation=initial_stock(database,session,product,row['incoming_quantity'],actor)
        session.flush()
        results.append(dict(row,product_id=product.id,stock_operation_id=operation))
    session.commit()
    from inventory_preview import invalidate_warehouse_cache
    for wid in {r['warehouse_id'] for r in results if r['action']!='existing'}:invalidate_warehouse_cache(wid)
    return dict(success=True,created_count=sum(r['action']=='create' for r in results),restored_count=sum(r['action']=='restore' for r in results),
        skipped_count=sum(r['action']=='existing' for r in results),total_initial_stock=sum(r['incoming_quantity'] for r in results),items=results)
