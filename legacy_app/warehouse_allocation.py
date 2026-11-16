"""Whole-order local calendar-day balancing in the stock write transaction."""
import json
from fractions import Fraction
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, text
from models import Product, Warehouse, WarehouseStock, Order, OrderItem
from stock_rules import sellable_quantity
import sales_pause


def migrate(connection):
    connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS order_allocations (
        order_id INTEGER PRIMARY KEY REFERENCES orders(id),
        warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
        allocation_mode TEXT NOT NULL, audit_json TEXT NOT NULL)''')


class CrossWarehouseRequired(ValueError):
    pass


def select_warehouse(session, items, user, *, now=None, cross_warehouse_confirmed=False,
                     explicit_warehouse_id=None):
    import config
    config.load_runtime_settings()
    from warehouse_policy import load
    policy=load(session)
    today=(now or datetime.now(timezone(timedelta(hours=8)))).astimezone(timezone(timedelta(hours=8)))
    day_start=today.replace(hour=0,minute=0,second=0,microsecond=0)
    start=day_start.astimezone(timezone.utc).replace(tzinfo=None)
    end=start+timedelta(days=1)
    needs={}
    for row in items:
        product=session.get(Product,int(row['product_id']))
        qty=int(row.get('quantity') or 0)
        if not product or product.is_parent or product.is_archived or not product.is_active or qty<=0:
            raise ValueError('目录商品不存在、已停售或数量无效')
        parent=product.parent
        if not parent or not parent.is_active or parent.is_archived:
            raise ValueError('商品分类不存在或已停售')
        key=(parent.name,product.name)
        needs[key]=needs.get(key,0)+qty
    candidates=[]
    categories=sorted({key[0] for key in needs})
    for warehouse in session.query(Warehouse).filter_by(is_active=True).order_by(Warehouse.id).all():
        if sales_pause.is_paused(getattr(config,'SALES_PAUSE_SECTION',None),warehouse.id):
            continue
        private_name=getattr(config,'WHOLESALE_PRIVATE_WAREHOUSE_NAME','')
        private_user=str(getattr(config,'WHOLESALE_PRIVATE_USERNAME','')).lstrip('@').casefold()
        if private_name and warehouse.name==private_name and str(user.username or '').lstrip('@').casefold()!=private_user:
            continue
        resolved=[]; available_total=0;coverage=[]
        for (category,name),quantity in needs.items():
            matches=[p for p in session.query(Product).filter_by(
                name=name,default_warehouse_id=warehouse.id,is_parent=False,
                is_active=True,is_archived=False).all()
                if p.parent and p.parent.name==category and p.parent.is_active and not p.parent.is_archived]
            if len(matches)!=1:
                break
            p=matches[0]
            stock=session.query(WarehouseStock).filter_by(warehouse_id=warehouse.id,product_id=p.id).first()
            available=sellable_quantity(int(stock.quantity or 0) if stock else 0,
                p.low_stock_threshold,getattr(config,'STOCK_AUTO_UNLIST_MODE','threshold'))
            if p.auto_unlisted or available<quantity:
                break
            available_total+=available
            coverage.append(Fraction(available,quantity))
            resolved.append(dict(product_id=p.id,warehouse_id=warehouse.id,category_id=p.parent_id,quantity=quantity))
        if len(resolved)!=len(needs):
            continue
        # Count today's actual assignments across ALL products, including manual
        # orders, once per original order per warehouse. Persisted times are UTC.
        quantity=session.execute(text('''SELECT COUNT(DISTINCT COALESCE(
            NULLIF(o.split_group_id,''),'order:' || o.id))
            FROM order_items a JOIN orders o ON o.id=a.order_id
            WHERE a.warehouse_id=:warehouse AND o.status!='cancelled'
              AND o.created_at>=:start AND o.created_at<:end '''),
            dict(warehouse=warehouse.id,start=start,end=end)).scalar()
        candidates.append(dict(warehouse_id=warehouse.id,warehouse_name=warehouse.name,items=resolved,
            daily_quantity=int(quantity or 0),available_stock=available_total,
            minimum_flavor_coverage=min(coverage)))
    if explicit_warehouse_id is not None:
        candidates=[c for c in candidates if c['warehouse_id']==int(explicit_warehouse_id)]
        if not candidates:
            raise ValueError('客户指定仓库无法完整履约，请检查库存或调整仓库要求')
    if not candidates:
        if len(needs) <= 1:
            raise ValueError('当前商品无可完整履约仓库，请检查库存和停售状态')
        resolved=[]; audits=[]
        for (category,name),quantity in needs.items():
            source=next(row for row in items if session.get(Product,int(row['product_id'])).name==name
                        and session.get(Product,int(row['product_id'])).parent.name==category)
            selected,_,audit=select_warehouse(session,[dict(source,quantity=quantity)],user,now=now)
            resolved.extend(selected); audits.append(audit)
        if not cross_warehouse_confirmed:
            raise CrossWarehouseRequired('当前订单产品存在于不同仓库，是否分单发货？')
        return resolved,'客户确认跨仓',dict(routes=audits)
    # Stock priority is explicit and dominates the ratio when enabled.
    best=max(c['minimum_flavor_coverage'] for c in candidates)
    comparable=[c for c in candidates if not policy['stock_priority'] or c['minimum_flavor_coverage']==best]
    weights={c['warehouse_id']:policy['weights'].get(str(c['warehouse_id']),
             6 if c['warehouse_name'].strip()=='一仓' else 4 if c['warehouse_name'].strip()=='二仓' else 1)
             for c in candidates}
    total=sum(c['daily_quantity'] for c in candidates)
    target_total=sum(weights.values())
    def ratio_error(chosen):
        return sum((Fraction(c['daily_quantity']+(c is chosen),total+1)-Fraction(weights[c['warehouse_id']],target_total))**2 for c in candidates)
    comparable.sort(key=lambda c:(ratio_error(c),c['warehouse_id']))
    selected=comparable[0]
    mode='唯一可履约' if len(candidates)==1 else ('库存优先，当日订单比例辅助' if policy['stock_priority'] else '全仓自然日订单比例')
    def public(c):
        return {k:(float(v) if isinstance(v,Fraction) else v) for k,v in c.items() if k!='items'}
    audit=dict(local_date=today.date().isoformat(),timezone='Asia/Shanghai',period='CALENDAR_DAY',
        local_day_start=day_start.isoformat(),local_day_end=(day_start+timedelta(days=1)).isoformat(),
        categories=categories,selected_warehouse_id=selected['warehouse_id'],
        ranking=mode,weights=weights,stock_priority=policy['stock_priority'],policy_revision=policy['revision'],
        stock_comparable_tolerance='0%',cancelled_excluded=True,split_groups_counted_once=True,
        candidates=[public(c) for c in candidates])
    return selected['items'],mode,audit


def record(session,order_id,warehouse_id,mode,audit):
    session.execute(text('''INSERT INTO order_allocations(order_id,warehouse_id,allocation_mode,audit_json)
        VALUES(:order_id,:warehouse_id,:mode,:audit)'''),dict(order_id=order_id,
        warehouse_id=warehouse_id,mode=mode,audit=json.dumps(audit,ensure_ascii=False)))
