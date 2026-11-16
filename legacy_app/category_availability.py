"""Category admission switch. Never changes child stock or historical orders."""
def set_status(session, identity, enabled, expected, actor):
    if type(enabled) is not bool or type(expected) is not bool or not actor:
        raise ValueError('分类状态或操作人无效')
    from models import Product
    from catalog_retirement import require_operating
    require_operating(session,'product',identity)
    category=session.get(Product,identity)
    if not category or not category.is_parent or category.is_archived:
        raise ValueError('分类不存在或已归档')
    if bool(category.is_active) != expected:
        raise ValueError('分类状态已变化，请刷新后操作')
    category.is_active=enabled
    # Existing generic product maintenance already treats is_active as the
    # authoritative live switch. Store an independent minimal audit atomically.
    from sqlalchemy import text
    session.execute(text('CREATE TABLE IF NOT EXISTS category_availability_audit '
        '(id INTEGER PRIMARY KEY, category_id INTEGER, enabled INTEGER, actor TEXT, at TEXT)'))
    from datetime import datetime,timezone
    session.execute(text('INSERT INTO category_availability_audit(category_id,enabled,actor,at) '
        'VALUES (:id,:enabled,:actor,:at)'),dict(id=identity,enabled=int(enabled),actor=actor,
                                               at=datetime.now(timezone.utc).isoformat()))
    session.commit()
    return dict(success=True,category_id=identity,is_active=enabled,stock_changed=False,orders_changed=False)
