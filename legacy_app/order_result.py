"""Accepted monetary terms and persisted order facts; never reprice receipts."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class ReconfirmationRequired(ValueError):
    def __init__(self, quote):
        super().__init__('仓库/价格发生变化，请重新确认；尚未创建订单或扣库存')
        self.quote = quote


def money(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError('确认金额无效')
        return str(number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError):
        raise ValueError('确认金额无效') from None


def monetary_terms(result):
    """Warehouse may change at equal terms; identity, quantity and all money may not."""
    if not isinstance(result, dict) or not result.get('items'):
        raise ValueError('缺少用户确认的商品/金额')
    rows = defaultdict(int)
    for item in result['items']:
        category = item.get('parent_name') or item.get('category_name') or ''
        key = (str(category), str(item['product_name']), money(item['unit_price']))
        quantity = int(item['quantity'])
        if quantity <= 0:
            raise ValueError('确认数量无效')
        rows[key] += quantity
    return {
        'items': [list(key) + [quantity] for key, quantity in sorted(rows.items())],
        **{key: money(result[key]) for key in ('total_amount', 'total_shipping_fee', 'grand_total')},
    }


def shipping_rows(database, package, shipping_mode):
    """The existing per-package shipping calculation, shared by quote and persistence."""
    total = 0.0
    rows = []
    if any(item.get('bundle_instance') is not None for item in package):
        ordinary=[item for item in package if item.get('bundle_instance') is None]
        fee, ordinary_rows=shipping_rows(database,ordinary,shipping_mode) if ordinary else (0.0,[])
        return fee,ordinary_rows+[dict(item,shipping_fee=0.0) for item in package if item.get('bundle_instance') is not None]
    if shipping_mode != 'warehouse':
        from parcel_shipping import total as parcel_total
        total=parcel_total(package)
        for index, item in enumerate(package):
            rows.append(dict(item, shipping_fee=total if index == 0 else 0.0))
    else:
        from warehouse_shipping import partition
        scoped_fee, remaining = partition(package)
        total += scoped_fee
        remaining_ids = {id(item) for item in remaining}
        used = set()
        for item in package:
            warehouse = item['warehouse']
            fee = 0.0
            if id(item) in remaining_ids and warehouse.id not in used:
                fee = float(warehouse.shipping_fee or 0)
                used.add(warehouse.id)
            total += fee
            rows.append(dict(item, shipping_fee=fee))
        if rows:
            rows[0]['shipping_fee'] += scoped_fee
    return total, rows


def quote_packages(database, packages, shipping_mode):
    """No writes/FIFO consumption; calculated while the final allocation lock is held."""
    grouped = {}
    total = 0.0
    shipping = 0.0
    packaged = []
    for package in packages:
        from order_trace import stage
        with stage('SHIPPING'):
            fee, rows = shipping_rows(database, package, shipping_mode)
        packaged.append((fee, rows))
        shipping += fee
        for item in rows:
            product, warehouse = item['product'], item['warehouse']
            key = (product.id, warehouse.id, item.get('bundle_instance'), item['unit_price'])
            row = grouped.setdefault(key, dict(product_id=product.id, product_name=product.name,
                parent_name=product.parent.name if product.parent else '',
                warehouse_id=warehouse.id, warehouse_name=warehouse.name,
                unit_price=item['unit_price'], quantity=0, subtotal=0.0, shipping_fee=0.0,
                unit=item.get('unit','支'),
                current_stock=item.get('current_stock', 0)))
            row['quantity'] += int(item['quantity'])
            subtotal = item['unit_price'] * int(item['quantity'])
            row['subtotal'] += subtotal
            row['shipping_fee'] += item['shipping_fee']
            total += subtotal
    return dict(items=list(grouped.values()), total_amount=total, total_shipping_fee=shipping,
        grand_total=total+shipping, package_count=len(packages),
        total_units=sum(row['quantity'] for row in grouped.values())), packaged


def persisted_item(session, item, remaining=None):
    from inventory_costs import item_summary
    product = item.product
    category = product.parent.name if product and product.parent else '默认分类'
    return dict(item_id=item.id,product_id=item.product_id, product_name=product.name if product else str(item.product_id),
        parent_name=category, category_name=category,
        warehouse_id=item.warehouse_id, warehouse_name=item.warehouse.name if item.warehouse else None,
        quantity=item.quantity, unit=__import__('product_units').historical(session,item), unit_price=item.unit_price, subtotal=item.quantity*item.unit_price,
        total_price=item.quantity*item.unit_price, shipping_fee=item.shipping_fee,
        remaining_quantity=remaining, **item_summary(session, item))
