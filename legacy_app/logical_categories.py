"""Read-only logical categories: exact canonical names, never fuzzy model aliases.

Same identity as rules_admin.logical_brands. Physical IDs remain immutable.
"""
from fastapi import HTTPException


def category_key(name):
    return str(name or '')


def active_parents(session):
    from models import Product
    from catalog_retirement import operating_query
    return operating_query(session,Product,'product').filter(Product.is_parent == True,
        Product.is_active == True, Product.is_archived == False).order_by(Product.id).all()


def category_ids(session, representative_id):
    parents = active_parents(session)
    selected = next((p for p in parents if p.id == int(representative_id)), None)
    if selected is None:
        raise HTTPException(400, '分类不存在、已禁用或已退役，请刷新分类列表')
    return [p.id for p in parents if category_key(p.name) == category_key(selected.name)]


def groups(session, products=None):
    from models import Product
    from catalog_retirement import operating_query, retired_ids
    parents = active_parents(session)
    by_name = {}
    for parent in parents:
        group = by_name.setdefault(category_key(parent.name), dict(id=parent.id,
            name=parent.name, category_ids=[], category_display_ids=[],
            warehouse_ids=[], product_ids=[], flavor_count=0, product_count=0))
        group['category_ids'].append(parent.id)
        if parent.category_serial:
            group['category_display_ids'].append('C'+str(parent.category_serial).zfill(3))
        if parent.default_warehouse_id not in group['warehouse_ids']:
            group['warehouse_ids'].append(parent.default_warehouse_id)
    by_parent = {id: group for group in by_name.values() for id in group['category_ids']}
    if products is None:
        products = operating_query(session,Product,'product').filter(Product.is_parent == False,
            Product.is_active == True, Product.is_archived == False,
            Product.parent_id.in_(list(by_parent))).order_by(Product.id).all()
    flavors = {}
    retired = set(retired_ids(session,'product'))
    for product in products:
        group = by_parent.get(product.parent_id)
        if not group or not product.is_active or product.is_archived or product.is_parent or product.id in retired:
            continue
        group['product_ids'].append(product.id)
        flavors.setdefault(group['id'], set()).add(category_key(product.name))
    for group in by_name.values():
        group['product_count'] = len(group['product_ids'])
        group['flavor_count'] = len(flavors.get(group['id'], set()))
    return list(by_name.values())


def filter_orders(query, session, representative_id, warehouse_id=None):
    from models import Order, OrderItem, Product
    from sqlalchemy import and_, or_
    ids = category_ids(session, representative_id)
    match = OrderItem.product.has(or_(Product.parent_id.in_(ids), Product.id.in_(ids)))
    if warehouse_id is not None:
        match = and_(match, OrderItem.warehouse_id == warehouse_id)
    # One EXISTS ensures warehouse and category match the SAME order item.
    return query.filter(Order.items.any(match))
