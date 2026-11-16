"""Atomic whole-batch inventory operations reusing existing FIFO cost lots.

Schema migration is explicit; importing this module never migrates a database.
"""
import json
import re
import uuid
import math
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.orm import Session
from models import Product, Warehouse, WarehouseStock, InventoryBatch, StockMovement


DDL = (
    '''CREATE TABLE IF NOT EXISTS stock_operations (
        id TEXT PRIMARY KEY, operation_type TEXT NOT NULL,
        warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
        category_id INTEGER NOT NULL REFERENCES products(id),
        status TEXT NOT NULL DEFAULT 'completed', actor TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        cancelled_at TEXT, cancelled_by TEXT, cancel_reason TEXT,
        total_quantity INTEGER NOT NULL, total_cost REAL,
        cost_status TEXT NOT NULL DEFAULT 'COST_COMPLETE')''',
    '''CREATE TABLE IF NOT EXISTS stock_operation_lines (
        id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES stock_operations(id),
        product_id INTEGER NOT NULL REFERENCES products(id), quantity INTEGER NOT NULL,
        before_quantity INTEGER NOT NULL, after_quantity INTEGER NOT NULL,
        unit_cost REAL, before_reference_cost REAL NOT NULL,
        after_reference_cost REAL NOT NULL, fifo_details TEXT NOT NULL DEFAULT '[]')''',
    'CREATE INDEX IF NOT EXISTS ix_stock_operations_warehouse_created ON stock_operations(warehouse_id,created_at)',
)


def migrate(connection, provenance_manifest=None):
    """Idempotent additive migration, to be tested on a DB copy before deployment."""
    for statement in DDL:
        connection.exec_driver_sql(statement)
    import inventory_costs
    inventory_costs.migrate(connection, provenance_manifest)


def parse_lines(raw, catalog):
    """Exact catalog names before compact suffix splitting; no fuzzy creation."""
    names = {}
    for p in catalog:
        names.setdefault(p.name, []).append(p)
    results, seen = [], set()
    for number,line in enumerate(str(raw or '').splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line in names or re.fullmatch(r'[gG]8', line):
            raise ValueError(f'第{number}行缺少独立数量，不能把型号拆成数量')
        separated = re.fullmatch(r'(.+?)\s+(\d+)(?:\s+(\d+(?:\.\d+)?))?', line)
        unit_cost = None
        if separated:
            name, qty, cost = separated.groups()
            unit_cost = float(cost) if cost is not None else None
            matches = names.get(name.strip(), [])
        else:
            candidates = [(name, line[len(name):]) for name in names
                          if line.startswith(name) and line[len(name):].isdigit()]
            if len(candidates) != 1:
                raise ValueError(f'第{number}行商品与数量无法唯一匹配')
            name, qty = candidates[0]
            matches = names[name]
        if len(matches) != 1 or int(qty) <= 0:
            raise ValueError(f'第{number}行商品不存在、重名或数量无效')
        p = matches[0]
        if p.id in seen:
            raise ValueError(f'第{number}行商品重复，请合并数量')
        seen.add(p.id)
        results.append(dict(product=p, quantity=int(qty), unit_cost=unit_cost,
                            line_no=number, match='目录精确匹配'))
    if not results:
        raise ValueError('请输入出入库商品明细')
    return results


def prepare(session, warehouse_id, category_id, raw, operation, actual_unit_cost=None):
    if operation not in ('inbound','outbound'):
        raise ValueError('操作类型无效')
    warehouse = session.get(Warehouse, warehouse_id)
    category = session.get(Product, category_id)
    if not warehouse or not warehouse.is_active:
        raise ValueError('仓库不存在或已停用')
    if (not category or not category.is_parent
            or category.default_warehouse_id != warehouse_id or category.is_archived):
        raise ValueError('分类不属于当前仓库')
    catalog = session.query(Product).filter(
        Product.default_warehouse_id == warehouse_id,
        Product.parent_id == category_id, Product.is_parent == False,
        Product.is_archived == False,
    ).all()
    rows = parse_lines(raw, catalog)
    missing_prices = []
    if actual_unit_cost is not None:
        try:
            if isinstance(actual_unit_cost, bool):
                raise ValueError('成本无效')
            actual_unit_cost = float(actual_unit_cost)
            if not math.isfinite(actual_unit_cost) or actual_unit_cost < 0:
                raise ValueError('成本无效')
        except (ValueError, TypeError):
            raise ValueError('本批实际入库成本必须为非负有限金额') from None
        if operation != 'inbound':
            raise ValueError('出库不能手工指定成本')
    for row in rows:
        row['cost_origin'] = '本批填写' if row['unit_cost'] is not None or actual_unit_cost is not None else None
        if actual_unit_cost is not None:
            if row['unit_cost'] is not None and row['unit_cost'] != actual_unit_cost:
                raise ValueError('行内成本与本批成本不同，请按不同实际成本分批入库')
            row['unit_cost'] = actual_unit_cost
        if row['unit_cost'] is not None and not math.isfinite(row['unit_cost']):
            raise ValueError('成本必须为有限金额')
        stock = session.query(WarehouseStock).filter_by(
            product_id=row['product'].id, warehouse_id=warehouse_id).first()
        before = int(stock.quantity or 0) if stock else 0
        after = before + (row['quantity'] if operation == 'inbound' else -row['quantity'])
        if after < 0:
            raise ValueError(f"第{row['line_no']}行库存不足，整批未执行")
        if operation == 'outbound' and row['unit_cost'] is not None:
            raise ValueError('出库成本由既有FIFO批次计算，不能手工覆盖')
        if operation == 'inbound' and row['unit_cost'] is None:
            from inventory_reference import reference_amount
            product = row['product']
            estimate = reference_amount(1, product.cost_price, product.reference_price_state)
            if estimate['reference_incomplete']:
                missing_prices.append(product.name)
            else:
                row['unit_cost'] = float(product.cost_price)
                row['cost_origin'] = '商品成本'
        row.update(stock=stock, before=before, after=after)
    if missing_prices:
        raise ValueError('整批未执行，以下商品缺少有效成本或价格待确认：' + '、'.join(missing_prices))
    return rows


def preview(database, warehouse_id, category_id, raw, operation, actual_unit_cost=None):
    from product_units import effective
    with database.SessionLocal() as session:
        rows = prepare(session, warehouse_id, category_id, raw, operation, actual_unit_cost)
        return [dict(product_id=r['product'].id, product_name=r['product'].name,
                     current_quantity=r['before'], quantity=r['quantity'],
                     resulting_quantity=r['after'], match=r['match'],
                     unit_cost=r['unit_cost'], cost_origin=r['cost_origin'],
                     unit=effective(session,r['product'])) for r in rows]


def execute(database, warehouse_id, category_id, raw, operation, actor, note='', failpoint=None, actual_unit_cost=None):
    """Stock, cost lots, movements, header and lines commit as one transaction."""
    with database.engine.connect() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        session = Session(bind=connection, autoflush=False)
        try:
            rows = prepare(session, warehouse_id, category_id, raw, operation, actual_unit_cost)
            op_id = 'STK-' + uuid.uuid4().hex
            total_cost = 0.0
            recorded = []
            for row in rows:
                p, qty = row['product'], row['quantity']
                reference_before = float(p.cost_price or 0)
                if operation == 'inbound':
                    result = database.inbound_stock(p.id, warehouse_id, qty,
                        unit_cost=row['unit_cost'], note=note, batch_no=op_id,
                        update_product_cost=False, session=session)
                    if not result.get('success'):
                        raise ValueError(result.get('error','入库失败'))
                    cost = result['unit_cost']
                    details = [{'batch_id':result['batch_id'], 'qty':qty, 'unit_cost':cost,
                                'cost_source':'VERIFIED_REAL_COST', 'cost_origin':row['cost_origin']}]
                else:
                    cost, _, details = database.consume_batches_fifo(session, p.id, warehouse_id, qty)
                    row['stock'].quantity = row['after']
                    session.add(StockMovement(product_id=p.id, warehouse_id=warehouse_id,
                        change_qty=-qty, movement_type='outbound', unit_cost=cost,
                        batch_no=op_id, ref_type='stock_operation', ref_id=op_id, note=note))
                    database.refresh_product_stock_status(session, p.id)
                if cost is None:
                    total_cost = None
                elif total_cost is not None:
                    total_cost += qty * cost
                # Display snapshot only; never change quantities or FIFO costs.
                from product_units import effective
                unit = effective(session,p)
                details = [dict(detail, display_unit=unit) for detail in details]
                recorded.append(dict(product_id=p.id, quantity=qty,
                    before_quantity=row['before'], after_quantity=row['after'],
                    unit_cost=cost, before_reference_cost=reference_before,
                    after_reference_cost=float(p.cost_price or 0),
                    fifo_details=json.dumps(details)))
            session.flush()
            if total_cost is not None:
                total_cost=round(total_cost,2)
            if failpoint:
                failpoint()
            connection.execute(text('''INSERT INTO stock_operations
                (id,operation_type,warehouse_id,category_id,status,actor,note,created_at,total_quantity,total_cost,cost_status)
                VALUES (:id,:type,:warehouse,:category,'completed',:actor,:note,:at,:quantity,:cost,:cost_status)'''),
                dict(id=op_id,type=operation,warehouse=warehouse_id,category=category_id,
                     actor=actor,note=note,at=datetime.utcnow().isoformat(),
                     quantity=sum(r['quantity'] for r in rows),cost=total_cost,
                     cost_status='COST_INCOMPLETE' if total_cost is None else 'COST_COMPLETE'))
            for row in recorded:
                connection.execute(text('''INSERT INTO stock_operation_lines
                    (operation_id,product_id,quantity,before_quantity,after_quantity,
                     unit_cost,before_reference_cost,after_reference_cost,fifo_details)
                    VALUES (:operation_id,:product_id,:quantity,:before_quantity,:after_quantity,
                     :unit_cost,:before_reference_cost,:after_reference_cost,:fifo_details)'''),
                    dict(operation_id=op_id,**row))
            connection.commit()
            from inventory_preview import invalidate_warehouse_cache
            invalidate_warehouse_cache(warehouse_id)
            return dict(id=op_id,operation_type=operation,total_quantity=sum(r['quantity'] for r in rows),
                        total_cost=total_cost,status='completed',
                        cost_status='COST_INCOMPLETE' if total_cost is None else 'COST_COMPLETE')
        except Exception:
            connection.rollback()
            raise
        finally:
            session.close()


def cancel(database, operation_id, actor, reason, failpoint=None):
    if not str(reason or '').strip():
        raise ValueError('取消原因不能为空')
    with database.engine.connect() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        session = Session(bind=connection, autoflush=False)
        try:
            op = connection.execute(text('SELECT * FROM stock_operations WHERE id=:id'),
                                    {'id':operation_id}).mappings().first()
            if not op or op['status'] != 'completed':
                raise ValueError('批次不存在或已经取消，禁止重复取消')
            lines = connection.execute(text('SELECT * FROM stock_operation_lines WHERE operation_id=:id ORDER BY id'),
                                       {'id':operation_id}).mappings().all()
            for line in lines:
                stock = session.query(WarehouseStock).filter_by(
                    product_id=line['product_id'],warehouse_id=op['warehouse_id']).first()
                p = session.get(Product,line['product_id'])
                if not stock or not p:
                    raise ValueError('原库存记录或商品缺失，不能取消')
                qty = line['quantity']
                if op['operation_type'] == 'inbound':
                    details = json.loads(line['fifo_details'])
                    lot = session.get(InventoryBatch,details[0]['batch_id'])
                    if not lot or lot.remain_qty != qty or stock.quantity < qty:
                        raise ValueError('原入库库存已经消耗，不能整笔取消')
                    lot.remain_qty = 0
                    stock.quantity -= qty
                    later = session.query(InventoryBatch).filter(
                        InventoryBatch.product_id == p.id,
                        InventoryBatch.warehouse_id == op['warehouse_id'],
                        InventoryBatch.id > lot.id,
                    ).first()
                    if not later and float(p.cost_price or 0) == line['after_reference_cost']:
                        p.cost_price = line['before_reference_cost']
                    change = -qty
                else:
                    from inventory_costs import resolve_details, BLOCKED
                    details = resolve_details(session,json.loads(line['fifo_details']))
                    for detail in details:
                        lot = session.get(InventoryBatch,detail['batch_id'])
                        if (not lot or lot.product_id!=p.id or lot.warehouse_id!=op['warehouse_id']
                                or lot.remain_qty+detail['qty']>lot.quantity):
                            raise ValueError(BLOCKED)
                        lot.remain_qty += detail['qty']
                    stock.quantity += qty
                    change = qty
                session.add(StockMovement(product_id=p.id,warehouse_id=op['warehouse_id'],
                    change_qty=change,movement_type='batch_cancel',unit_cost=line['unit_cost'],
                    ref_type='stock_operation',ref_id=operation_id,note=reason))
                database.refresh_product_stock_status(session,p.id)
            session.flush()
            if failpoint:
                failpoint()
            connection.execute(text("UPDATE stock_operations SET status='cancelled', cancelled_at=:at, cancelled_by=:actor, cancel_reason=:reason WHERE id=:id AND status='completed'"),
                dict(at=datetime.utcnow().isoformat(),actor=actor,reason=reason,id=operation_id))
            connection.commit()
            from inventory_preview import invalidate_warehouse_cache
            invalidate_warehouse_cache(op['warehouse_id'])
            return dict(id=operation_id,status='cancelled')
        except Exception:
            connection.rollback()
            raise
        finally:
            session.close()
