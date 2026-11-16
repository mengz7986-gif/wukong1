"""Safe current catalog reassignment. Historical facts are never moved."""
from sqlalchemy import func
from models import Product, Warehouse, WarehouseStock, InventoryBatch


def move_plan(session, category_id, warehouse_id, name=None):
    category=session.get(Product,category_id)
    warehouse=session.get(Warehouse,warehouse_id)
    if not category or not category.is_parent or category.is_archived:
        raise ValueError('分类不存在')
    if not warehouse or not warehouse.is_active:
        raise ValueError('请选择有效仓库')
    target_name=(name or category.name).strip()
    duplicate=session.query(Product).filter(Product.is_parent==True,Product.is_archived==False,
        Product.id!=category.id,Product.name==target_name,Product.default_warehouse_id==warehouse_id).first()
    if duplicate:
        raise ValueError('目标仓库已存在同名分类，不能自动合并')
    children=session.query(Product).filter(Product.parent_id==category.id).all()
    ids=[category.id]+[p.id for p in children]
    moving=category.default_warehouse_id!=warehouse_id
    if moving:
        stock=session.query(WarehouseStock).filter(WarehouseStock.product_id.in_(ids),WarehouseStock.quantity!=0).first()
        lots=session.query(InventoryBatch).filter(InventoryBatch.product_id.in_(ids),InventoryBatch.remain_qty>0).first()
        if stock or lots:
            raise ValueError('分类或商品仍有库存/未消耗成本批次，请先处理库存；禁止直接换仓')
    return category,children,dict(category_id=category.id,product_count=len(children),
        from_warehouse_id=category.default_warehouse_id,to_warehouse_id=warehouse_id,moving=moving)


def apply_move(session,category_id,warehouse_id,name=None):
    category,children,plan=move_plan(session,category_id,warehouse_id,name)
    if plan['moving']:
        category.default_warehouse_id=warehouse_id
        for product in children:
            product.default_warehouse_id=warehouse_id
    return plan
