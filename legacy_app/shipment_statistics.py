"""Created-order cohort shared by operating summary and item breakdown."""
from datetime import datetime, timedelta
from decimal import Decimal
from collections import defaultdict
from sqlalchemy.orm import selectinload

def bounds(start, end):
    from business_time_range import bounds as utc_bounds
    return utc_bounds(start,end,required=True)

def calculate(database, start, end, warehouse_id=None):
    from models import Order, OrderItem, Product, Warehouse
    from inventory_costs import order_summary
    from inventory_reference import reference_summary
    first, last = bounds(start, end)
    money = lambda value: Decimal(str(value or 0))
    with database.SessionLocal() as session:
        if warehouse_id is not None and not session.get(Warehouse, warehouse_id):
            raise ValueError('仓库不存在')
        query = session.query(Order).options(selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.parent))
        query = query.filter(Order.created_at >= first, Order.created_at < last, Order.status != 'cancelled')
        if warehouse_id is not None:
            query = query.filter(Order.items.any(OrderItem.warehouse_id == warehouse_id))
        orders = query.all()
        rows = {}
        sales = Decimal(0); shipping = Decimal(0); quantity = 0
        costs = []
        for order in orders:
            selected = [i for i in order.items if warehouse_id is None or i.warehouse_id == warehouse_id]
            # A child order has one fulfillment warehouse. Refuse ambiguous
            # legacy shipping allocation instead of inventing a warehouse fee.
            if warehouse_id is not None and len(selected) != len(order.items):
                raise ValueError('历史订单包含多仓明细，运费无法可靠归属；请使用全部仓库查询')
            shipping += money(order.total_shipping_fee)
            costs.append(order_summary(session, order))
            for item in selected:
                product = item.product; parent = product.parent if product else None
                key = (parent.id if parent else None, item.product_id)
                row = rows.setdefault(key, dict(parent_id=key[0], parent_name=parent.name if parent else '未分类',
                    product_id=item.product_id, product_name=product.name if product else str(item.product_id),
                    quantity=0, sales_amount=Decimal(0), _order_ids=set()))
                qty = int(item.quantity or 0)
                if qty > 0:
                    row['_order_ids'].add(order.id)
                row['quantity'] += qty; quantity += qty
                amount = money(item.unit_price) * qty
                row['sales_amount'] += amount; sales += amount
        categories = {}
        for row in rows.values():
            row['sales_amount'] = float(row['sales_amount'])
            group = categories.setdefault(row['parent_id'], dict(id=row['parent_id'], name=row['parent_name'], quantity=0, items=[], _order_ids=set()))
            group['_order_ids'].update(row['_order_ids'])
            row['orders_count']=len(row.pop('_order_ids'))
            group['quantity'] += row['quantity']; group['items'].append(row)
        groups = sorted(categories.values(), key=lambda g: (-g['quantity'], g['name']))
        for group in groups:
            group['orders_count']=len(group.pop('_order_ids'))
            group['items'].sort(key=lambda i: (-i['quantity'], i['product_name']))
        assert sum(g['quantity'] for g in groups) == quantity
        reference = database.current_inventory_reference()
        if warehouse_id is not None:
            warehouses = [w for w in reference['warehouses'] if w['id'] == warehouse_id]
            reference = reference_summary([]) if not warehouses else dict(warehouses[0])
            reference['warehouses'] = warehouses
        known_cost = sum(money(c['known_cost_amount']) for c in costs)
        incomplete = any(c['cost_status'] != 'COST_COMPLETE' for c in costs)
        bundle_sales=[]
        from sqlalchemy import inspect
        if inspect(session.connection()).has_table('bundle_sales'):
            from bundles import receipt_sales
            totals={}
            for sale in receipt_sales(session,[o.id for o in orders]):
                row=totals.setdefault(sale['bundle_id'],dict(id=sale['bundle_id'],name=sale['name'],quantity=0,amount=Decimal(0),_order_ids=set()))
                row['quantity']+=sale['quantity'];row['amount']+=money(sale['amount'])
                row['_order_ids'].update(sale.get('order_ids') or ([sale['order_id']] if sale.get('order_id') else []))
            bundle_sales=[]
            for row in sorted(totals.values(),key=lambda r:(-r['quantity'],r['name'])):
                row['orders_count']=len(row.pop('_order_ids'))
                row['amount']=float(row['amount'])
                bundle_sales.append(row)
        return dict(label=f"{start.replace('T', ' ')} 至 {end.replace('T', ' ')}（北京时间）", timezone='Asia/Shanghai', orders_count=len(orders),
            orders_amount=float(sales), shipping_amount=float(shipping), grand_amount=float(sales + shipping),
            shipped_count=len(orders), shipped_quantity=quantity, categories=groups,
            time_basis='created_at', quantity_basis='valid_order_items',
            cost_amount=None if incomplete else float(known_cost), known_cost_amount=float(known_cost),
            profit_amount=None if incomplete else float(sales-known_cost), stock_reference=reference,
            bundle_sales=bundle_sales)
