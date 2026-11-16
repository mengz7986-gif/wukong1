"""Per original order flavor limits, shared by existing warehouse equivalents."""
import json
from collections import defaultdict
from datetime import datetime, timezone
from sqlalchemy import text


def migrate(connection):
    connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS product_order_limits (
        product_id INTEGER PRIMARY KEY REFERENCES products(id),
        maximum INTEGER NOT NULL CHECK(maximum>0))''')
    connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS product_order_limit_audit (
        id INTEGER PRIMARY KEY, actor TEXT NOT NULL, product_ids TEXT NOT NULL,
        maximum INTEGER, created_at TEXT NOT NULL)''')


def identity(product):
    if isinstance(product,dict):
        return (product.get('parent_name') or '', product['name'])
    return (product.parent.name if product.parent else '',product.name)


def read(session):
    # Newly added warehouse copies inherit the existing logical flavor limit.
    # Use the same parent-name/flavor identity as warehouse allocation.
    return dict(session.execute(text('''SELECT p.id,MIN(l.maximum)
        FROM products p JOIN products parent ON parent.id=p.parent_id
        JOIN products peer ON peer.name=p.name AND peer.is_parent=0 AND peer.is_archived=0
        JOIN products peer_parent ON peer_parent.id=peer.parent_id AND peer_parent.name=parent.name
        JOIN product_order_limits l ON l.product_id=peer.id
        WHERE p.is_parent=0 AND p.is_archived=0 GROUP BY p.id''')).all())


def set_limit(session,product,raw,actor):
    from models import Product
    value=str(raw).strip()
    if value and (not value.isascii() or not value.isdigit() or int(value)<1 or int(value)>100000):
        raise ValueError('每单上限须为1至100000的整数；留空表示不限购')
    if product.is_parent or product.is_archived:
        raise ValueError('请在未归档的具体口味上设置每单上限')
    siblings=[p for p in session.query(Product).filter_by(name=product.name,is_parent=False,is_archived=False)
              if identity(p)==identity(product)]
    maximum=int(value) if value else None
    for p in siblings:
        session.execute(text('DELETE FROM product_order_limits WHERE product_id=:id'),{'id':p.id})
        if maximum:
            session.execute(text('INSERT INTO product_order_limits(product_id,maximum) VALUES(:id,:m)'),{'id':p.id,'m':maximum})
    session.execute(text('INSERT INTO product_order_limit_audit(actor,product_ids,maximum,created_at) VALUES(:a,:p,:m,:t)'),
        {'a':str(actor),'p':json.dumps([p.id for p in siblings]),'m':maximum,'t':datetime.now(timezone.utc).isoformat()})
    return maximum


def violations(items,catalog):
    by_id={p['id']:p for p in catalog}; totals=defaultdict(int); limits={}
    for p in catalog:
        limit=p.get('max_per_order')
        if type(limit) is int and limit>0:
            key=identity(p);limits[key]=min(limits.get(key,limit),limit)
    for item in items:
        p=by_id.get(int(item['product_id']))
        if not p:raise ValueError('限购校验无法确认商品身份')
        quantity=item['quantity']
        if type(quantity) is not int or quantity<=0:raise ValueError('商品数量须为正整数')
        totals[identity(p)]+=quantity
    return [(brand,name,count,limits[(brand,name)]) for (brand,name),count in totals.items()
            if (brand,name) in limits and count>limits[(brand,name)]]


def rejection(rows):
    return '❌ 录单失败\n'+'\n'.join(f'• {brand}·{name}每单最多{limit}支，本次填写{count}支。'
        for brand,name,count,limit in rows)+'\n请修改数量后重新发送完整订单。'


def validate(session,items):
    from models import Product
    limits=read(session)
    catalog=[dict(id=p.id,name=p.name,parent_name=identity(p)[0],max_per_order=limits.get(p.id))
             for p in session.query(Product).filter_by(is_parent=False,is_archived=False)]
    errors=violations(items,catalog)
    if errors:raise ValueError(rejection(errors))
