"""Candidate independent bundle inventory; never touches product stock/FIFO.

Not wired into production ordering yet. Caller owns commit/rollback. Inventory
mutation requires the existing BEGIN IMMEDIATE order/admin transaction; guarded
SQL updates additionally reject overselling. Money is stored as integer cents.
"""
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import text

MODES = ('components', 'independent')
TABLES=('bundle_inventory_settings','bundle_inventory_batches','bundle_inventory_movements',
        'bundle_inventory_allocations','bundle_inventory_restorations','bundle_inventory_sale_links')


def migrate(connection):
    for sql in (
        '''CREATE TABLE IF NOT EXISTS bundle_inventory_settings (
        bundle_id INTEGER PRIMARY KEY REFERENCES bundle_definitions(id),
        mode TEXT NOT NULL DEFAULT 'components' CHECK(mode IN ('components','independent')),
        cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents>=0))''',
        '''CREATE TABLE IF NOT EXISTS bundle_inventory_batches (
        id INTEGER PRIMARY KEY, bundle_id INTEGER NOT NULL REFERENCES bundle_definitions(id),
        remaining INTEGER NOT NULL CHECK(remaining>=0), original_quantity INTEGER NOT NULL CHECK(original_quantity>0),
        cost_cents INTEGER NOT NULL CHECK(cost_cents>=0), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS bundle_inventory_movements (
        id INTEGER PRIMARY KEY, bundle_id INTEGER NOT NULL REFERENCES bundle_definitions(id),
        operation_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL CHECK(kind IN ('in','adjust_out','sale','cancel')),
        quantity INTEGER NOT NULL, cost_cents INTEGER NOT NULL,
        actor TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS bundle_inventory_allocations (
        movement_id INTEGER NOT NULL REFERENCES bundle_inventory_movements(id),
        batch_id INTEGER NOT NULL REFERENCES bundle_inventory_batches(id),
        quantity INTEGER NOT NULL CHECK(quantity>0), PRIMARY KEY(movement_id,batch_id))''',
        '''CREATE TABLE IF NOT EXISTS bundle_inventory_restorations (
        sale_movement_id INTEGER PRIMARY KEY REFERENCES bundle_inventory_movements(id),
        cancel_movement_id INTEGER NOT NULL UNIQUE REFERENCES bundle_inventory_movements(id))''',
        '''CREATE TABLE IF NOT EXISTS bundle_inventory_sale_links (
        bundle_sale_id INTEGER PRIMARY KEY REFERENCES bundle_sales(id),
        movement_id INTEGER NOT NULL UNIQUE REFERENCES bundle_inventory_movements(id))''',
    ):
        connection.execute(text(sql))


def cents(value):
    try:
        n=Decimal(str(value))
        if not n.is_finite() or n<0:raise ValueError()
        return int(n.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)*100)
    except Exception:
        raise ValueError('福袋进货价须为有效非负金额') from None


def quantity(value):
    if isinstance(value,bool) or not str(value).isdigit() or int(value)<=0:
        raise ValueError('福袋份数须为正整数')
    return int(value)


def view(session,bundle_id):
    from sqlalchemy import inspect
    if not inspect(session.connection()).has_table('bundle_inventory_settings'):
        return {'mode':'components','cost_cents':0,'remaining':0}
    row=session.execute(text('SELECT mode,cost_cents FROM bundle_inventory_settings WHERE bundle_id=:b'),{'b':bundle_id}).first()
    return {'mode':row[0] if row else 'components','cost_cents':row[1] if row else 0,
        'remaining':session.execute(text('SELECT COALESCE(SUM(remaining),0) FROM bundle_inventory_batches WHERE bundle_id=:b'),{'b':bundle_id}).scalar()}


def order_links(session,order_id):
    from sqlalchemy import inspect
    if not inspect(session.connection()).has_table('bundle_inventory_sale_links'):return {},set()
    links=dict(session.execute(text('SELECT s.id,l.movement_id FROM bundle_sales s JOIN bundle_inventory_sale_links l ON l.bundle_sale_id=s.id WHERE s.order_id=:o'),{'o':order_id}).all())
    items={r[0] for r in session.execute(text('SELECT f.order_item_id FROM bundle_fulfillment f JOIN bundle_sales s ON s.id=f.sale_id JOIN bundle_inventory_sale_links l ON l.bundle_sale_id=s.id WHERE s.order_id=:o'),{'o':order_id})}
    return links,items


def capacity(session,bundle):
    """Complete-portion capacity, never stitch a portion across warehouses.

    Management aggregate is not a user-specific quote; private/paused warehouse
    permissions are still rechecked at preview and commit.
    """
    from models import Product,Warehouse,WarehouseStock
    from stock_rules import sellable_quantity
    import config,sales_pause,json
    inventory=view(session,bundle['id'])
    if inventory['mode']=='independent':return inventory['remaining']
    cfg=bundle['config']
    if isinstance(cfg,str):cfg=json.loads(cfg)
    cat=session.get(Product,bundle['category_id'])
    if not cat or not cat.is_active or cat.is_archived:return 0
    ids=[r['product_id'] for r in cfg['components']] if cfg['mode']=='fixed' else cfg['pool']
    sources={pid:session.get(Product,pid) for pid in ids}
    total=0
    for w in session.query(Warehouse).filter_by(is_active=True):
        if sales_pause.is_paused(getattr(config,'SALES_PAUSE_SECTION',None),w.id):continue
        available={}
        for pid,p in sources.items():
            if not p or not p.is_active or p.is_archived:available[pid]=0;continue
            matches=[r for r in session.query(Product).filter_by(name=p.name,default_warehouse_id=w.id,is_parent=False,is_active=True,is_archived=False) if r.parent and r.parent.name==cat.name and r.parent.is_active and not r.parent.is_archived]
            if len(matches)!=1:available[pid]=0;continue
            target=matches[0];stock=session.query(WarehouseStock).filter_by(product_id=target.id,warehouse_id=w.id).first()
            available[pid]=0 if target.auto_unlisted else sellable_quantity(stock.quantity if stock else 0,target.low_stock_threshold,getattr(config,'STOCK_AUTO_UNLIST_MODE','threshold'))
        if cfg['mode']=='fixed':
            total+=min((available[r['product_id']]//r['quantity'] for r in cfg['components']),default=0)
        else:
            cap=cfg['max_per_flavor'] if cfg['allow_repeat'] else 1
            lo,hi=0,sum(available.values())//cfg['units']
            while lo<hi:
                mid=(lo+hi+1)//2
                if sum(min(q,mid*cap) for q in available.values())>=mid*cfg['units']:lo=mid
                else:hi=mid-1
            total+=lo
    return total


def item_cost(session,item):
    """Verify independent order cost against the immutable sale movement."""
    import json
    row=session.execute(text('SELECT m.id,m.cost_cents,s.snapshot FROM bundle_fulfillment f JOIN bundle_inventory_sale_links l ON l.bundle_sale_id=f.sale_id JOIN bundle_inventory_movements m ON m.id=l.movement_id JOIN bundle_sales s ON s.id=f.sale_id WHERE f.order_item_id=:i AND m.kind=\'sale\''),{'i':item.id}).first()
    if not row:raise ValueError('独立福袋成本缺少销售流水依据')
    proof=session.execute(text('SELECT oi.quantity,oi.fifo_details FROM order_items oi JOIN bundle_fulfillment f ON f.order_item_id=oi.id JOIN bundle_inventory_sale_links l ON l.bundle_sale_id=f.sale_id WHERE l.movement_id=:m'),{'m':row[0]}).all()
    total=Decimal(0);units=0
    for q,raw in proof:
        details=json.loads(raw)
        if sum(d['qty'] for d in details)!=q:raise ValueError('独立福袋成本数量不守恒')
        for d in details:
            if d.get('cost_source')!='BUNDLE_INDEPENDENT' or d.get('bundle_movement_id')!=row[0]:raise ValueError('独立成本来源不一致')
            value=Decimal(str(d['unit_cost']))
            if not value.is_finite() or value<0:raise ValueError('独立成本金额无效')
            total+=d['qty']*value;units+=d['qty']
    if units!=json.loads(row[2])['config']['units'] or total*100!=row[1]:raise ValueError('独立福袋成本流水不守恒')
    details=json.loads(item.fifo_details)
    return float(sum(Decimal(str(d['unit_cost']))*d['qty'] for d in details))


def configure(session,bundle_id,mode,cost):
    if mode not in MODES:raise ValueError('请选择一种福袋库存模式')
    if not session.execute(text('SELECT id FROM bundle_definitions WHERE id=:b AND deleted=0'),{'b':bundle_id}).scalar():
        raise ValueError('福袋不存在')
    current=view(session,bundle_id)
    if current['mode']!=mode:
        for table in ('bundle_sales','bundle_inventory_movements'):
            if session.execute(text('SELECT count(*) FROM '+table+' WHERE bundle_id=:b'),{'b':bundle_id}).scalar():
                raise ValueError('已有成交或库存流水，禁止直接切换福袋库存模式')
        if current['remaining']:raise ValueError('仍有独立库存余额，禁止直接切换模式')
    session.execute(text('INSERT INTO bundle_inventory_settings(bundle_id,mode,cost_cents) VALUES (:b,:m,:c) ON CONFLICT(bundle_id) DO UPDATE SET mode=excluded.mode,cost_cents=excluded.cost_cents'),{'b':bundle_id,'m':mode,'c':cents(cost)})


def _existing(session,key,bundle_id,kind,qty,cost=None):
    row=session.execute(text('SELECT * FROM bundle_inventory_movements WHERE operation_key=:k'),{'k':key}).mappings().first()
    if row and (row['bundle_id']!=bundle_id or row['kind']!=kind or row['quantity']!=qty or (cost is not None and row['cost_cents']!=cost)):
        raise ValueError('重复操作标识的内容不一致')
    return row


def _movement(session,bundle_id,key,kind,qty,cost,actor,note):
    if not key or not actor:raise ValueError('库存操作须有唯一标识及操作来源')
    return session.execute(text('INSERT INTO bundle_inventory_movements(bundle_id,operation_key,kind,quantity,cost_cents,actor,note) VALUES (:b,:k,:t,:q,:c,:a,:n)'),{'b':bundle_id,'k':key,'t':kind,'q':qty,'c':cost,'a':actor,'n':note}).lastrowid


def receive(session,bundle_id,count,cost,*,key,actor,note=''):
    count=quantity(count);unit=cents(cost)
    old=_existing(session,key,bundle_id,'in',count,count*unit)
    if old:return old['id']
    if view(session,bundle_id)['mode']!='independent':raise ValueError('仅独立库存模式可以入库')
    mid=_movement(session,bundle_id,key,'in',count,count*unit,actor,note)
    batch=session.execute(text('INSERT INTO bundle_inventory_batches(bundle_id,remaining,original_quantity,cost_cents) VALUES (:b,:q,:q,:c)'),{'b':bundle_id,'q':count,'c':unit}).lastrowid
    session.execute(text('INSERT INTO bundle_inventory_allocations VALUES (:m,:b,:q)'),{'m':mid,'b':batch,'q':count})
    return mid


def consume(session,bundle_id,count,*,key,actor,kind='sale',note=''):
    count=quantity(count)
    if kind not in ('sale','adjust_out'):raise ValueError('出库类型无效')
    old=_existing(session,key,bundle_id,kind,-count)
    if old:return dict(old)
    if view(session,bundle_id)['mode']!='independent':raise ValueError('当前不是独立库存模式')
    rows=session.execute(text('SELECT id,remaining,cost_cents FROM bundle_inventory_batches WHERE bundle_id=:b AND remaining>0 ORDER BY id'),{'b':bundle_id}).all()
    if sum(r[1] for r in rows)<count:raise ValueError('福袋独立库存不足，整单未成交')
    left=count;alloc=[];cost=0
    for bid,remaining,unit in rows:
        n=min(left,remaining);alloc.append((bid,n));cost+=n*unit;left-=n
        if not left:break
    # A savepoint also restores already-decremented batches on any CAS conflict.
    with session.begin_nested():
        mid=_movement(session,bundle_id,key,kind,-count,cost,actor,note)
        for bid,n in alloc:
            changed=session.execute(text('UPDATE bundle_inventory_batches SET remaining=remaining-:q WHERE id=:b AND remaining>=:q'),{'b':bid,'q':n})
            if changed.rowcount!=1:raise ValueError('库存已变化，请刷新后重试')
            session.execute(text('INSERT INTO bundle_inventory_allocations VALUES (:m,:b,:q)'),{'m':mid,'b':bid,'q':n})
    return {'id':mid,'bundle_id':bundle_id,'quantity':-count,'cost_cents':cost,'kind':kind}


def restore(session,sale_id,*,key,actor):
    sale=session.execute(text("SELECT * FROM bundle_inventory_movements WHERE id=:i AND kind='sale'"),{'i':sale_id}).mappings().first()
    if not sale:raise ValueError('找不到独立福袋成交库存依据')
    done=session.execute(text('SELECT cancel_movement_id FROM bundle_inventory_restorations WHERE sale_movement_id=:i'),{'i':sale_id}).scalar()
    if done:return done
    old=_existing(session,key,sale['bundle_id'],'cancel',-sale['quantity'],sale['cost_cents'])
    if old:raise ValueError('取消标识已关联其他库存恢复')
    with session.begin_nested():
        mid=_movement(session,sale['bundle_id'],key,'cancel',-sale['quantity'],sale['cost_cents'],actor,'正常取消恢复原批次')
        session.execute(text('INSERT INTO bundle_inventory_restorations VALUES (:s,:c)'),{'s':sale_id,'c':mid})
        for bid,n in session.execute(text('SELECT batch_id,quantity FROM bundle_inventory_allocations WHERE movement_id=:m'),{'m':sale_id}):
            changed=session.execute(text('UPDATE bundle_inventory_batches SET remaining=remaining+:q WHERE id=:b AND remaining+:q<=original_quantity'),{'b':bid,'q':n})
            if changed.rowcount!=1:raise ValueError('原库存批次无法安全恢复')
            session.execute(text('INSERT INTO bundle_inventory_allocations VALUES (:m,:b,:q)'),{'m':mid,'b':bid,'q':n})
    return mid
