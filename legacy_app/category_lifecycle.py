"""Recoverable category deletion; historical identities are never rewritten."""
import json
from datetime import datetime, timezone
from sqlalchemy import text


def restore_or_create(session, name, warehouse_id):
    """Reuse a sole archived identity, never resurrect descendants or stock."""
    from models import Product, WarehouseStock, InventoryBatch
    rows=session.query(Product).filter_by(name=name,is_parent=True,
        default_warehouse_id=warehouse_id).order_by(Product.id).all()
    live=[p for p in rows if not p.is_archived]
    if live:
        raise ValueError('当前仓库已有未归档同名分类（ID：%s），不能重复创建' % ','.join(str(p.id) for p in live))
    if len(rows)>1:
        raise ValueError('当前仓库存在多个归档同名分类（ID：%s），无法安全确定恢复对象' % ','.join(str(p.id) for p in rows))
    if not rows:return None
    parent=rows[0]
    from catalog_retirement import retired_ids
    if parent.id in retired_ids(session, 'product'):
        raise ValueError('验收分类已永久退出经营目录，不能恢复')
    children=session.query(Product).filter_by(parent_id=parent.id).all()
    if any(not p.is_archived for p in children):
        raise ValueError('归档分类仍有关联未归档口味，请先核对目录状态，不能自动恢复')
    if any(p.default_warehouse_id!=warehouse_id for p in children):
        raise ValueError('归档分类下口味的仓库归属不一致，不能自动恢复')
    ids=[parent.id]+[p.id for p in children]
    if any(int(s.quantity or 0)!=0 for s in session.query(WarehouseStock).filter(WarehouseStock.product_id.in_(ids))):
        raise ValueError('归档分类或下属口味仍有库存，不能自动恢复')
    if any(int(b.remain_qty or 0)!=0 for b in session.query(InventoryBatch).filter(InventoryBatch.product_id.in_(ids))):
        raise ValueError('归档分类仍有未消耗进货批次，不能自动恢复')
    if session.bind.dialect.has_table(session.connection(),'inventory_unknown_cost'):
        from sqlalchemy import bindparam
        statement=text('SELECT COUNT(*) FROM inventory_unknown_cost WHERE product_id IN :ids AND remain_qty<>0').bindparams(bindparam('ids',expanding=True))
        if session.execute(statement,{'ids':ids}).scalar():
            raise ValueError('归档分类仍有历史库存层，不能自动恢复')
    parent.is_archived=False;parent.is_active=True;parent.auto_unlisted=False
    # Prices, aliases (identity references), description and child IDs stay intact.
    return parent


def blockers(session, parent):
    from models import Product, WarehouseStock, InventoryBatch
    children=session.query(Product).filter_by(parent_id=parent.id).all()
    reasons=[]
    live=[p for p in children if not p.is_archived]
    if live: reasons.append('分类下还有 %d 个未归档商品，请先处理商品' % len(live))
    ids=[parent.id]+[p.id for p in children]
    quantity=sum(int(s.quantity or 0) for s in session.query(WarehouseStock)
                 .filter(WarehouseStock.product_id.in_(ids)))
    if any(int(s.quantity or 0)!=0 for s in session.query(WarehouseStock)
           .filter(WarehouseStock.product_id.in_(ids))):
        reasons.append('分类关联库存非零（合计 %d 件），不能删除' % quantity)
    if any(int(b.remain_qty or 0)>0 for b in session.query(InventoryBatch)
           .filter(InventoryBatch.product_id.in_(ids))):
        reasons.append('分类仍有关联未消耗成本批次，请先核对库存与成本')
    import warehouse_shipping
    if any(int(r.get('category_id',0))==parent.id for r in warehouse_shipping.read()['rules'].values()):
        reasons.append('分类仍有独立运费配置，请先恢复默认运费')
    import rule_dictionary
    if any(r.get('category_id')==parent.id and r.get('status')=='active'
           for r in rule_dictionary.read_store().get('aliases',[])):
        reasons.append('分类仍被已启用人工叫法引用，请先停用')
    return reasons


def archive(database, parent_id, actor):
    from models import Product
    with database.SessionLocal() as session:
        # Serialize deletion with SQLite product/stock writes and recheck at commit.
        session.execute(text('BEGIN IMMEDIATE'))
        parent=session.query(Product).filter_by(id=parent_id,is_parent=True).first()
        if not parent: raise ValueError('分类不存在')
        if parent.is_archived: raise ValueError('分类已经删除，请勿重复操作')
        reasons=blockers(session,parent)
        if reasons: raise ValueError('；'.join(reasons))
        stamp={'action':'category_archive','actor':actor,
               'at':datetime.now(timezone.utc).isoformat(),'category_id':parent.id,
               'warehouse_id':parent.default_warehouse_id,'name':parent.name}
        # Existing description retains an append-only audit; no schema migration.
        parent.description=(parent.description or '')+'\n[分类删除审计] '+json.dumps(stamp,ensure_ascii=False)
        parent.is_archived=True;parent.is_active=False
        session.commit()
        return {'success':True,'message':'分类已安全删除，历史记录保留',
                'parent_id':parent_id,'moved_count':0}


def assert_available_default(parent):
    if parent is not None and parent.is_archived:
        raise ValueError('默认分类已删除，请明确选择一个有效分类；系统不会自动重建已删除分类')
    return parent
