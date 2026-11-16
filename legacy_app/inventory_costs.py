"""Historical cost provenance. Unknown quantities are NOT priced inventory batches.

Migrations are explicit and additive. No price or physical stock is ever changed
by classification or supplementation. All writers use the caller's transaction.
"""
import json
import math
from datetime import datetime
from sqlalchemy import text, inspect

INCOMPLETE = 'COST_INCOMPLETE'
VERIFIED = ('VERIFIED_REAL_COST', 'RESTORED_VERIFIED_FIFO')
UNTRUSTED = ('REFERENCE_PRICE_FALLBACK', 'COST_UNVERIFIED', 'MISSING')
BLOCKED = '该操作涉及历史进货价缺失，无法安全恢复原批次；本次操作未执行。'
DDL = (
    '''CREATE TABLE IF NOT EXISTS inventory_provenance_segments (
       id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES inventory_batches(id),
       segment_index INTEGER NOT NULL, source_state TEXT NOT NULL
         CHECK(source_state IN ('REFERENCE_PRICE_FALLBACK','COST_UNVERIFIED')),
       quantity INTEGER NOT NULL CHECK(quantity>0), original_unit_cost REAL NOT NULL,
       evidence TEXT NOT NULL, created_at TEXT NOT NULL,
       UNIQUE(batch_id,segment_index))''',
    '''CREATE TABLE IF NOT EXISTS inventory_unknown_cost (
       id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
       warehouse_id INTEGER NOT NULL REFERENCES warehouses(id), quantity INTEGER NOT NULL,
       remain_qty INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'COST_INCOMPLETE',
       created_at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT, evidence TEXT)''',
    '''CREATE TABLE IF NOT EXISTS inventory_cost_resolution (
       id INTEGER PRIMARY KEY, layer_id INTEGER NOT NULL REFERENCES inventory_unknown_cost(id),
       batch_id INTEGER NOT NULL REFERENCES inventory_batches(id),
       start_offset INTEGER NOT NULL, quantity INTEGER NOT NULL)''',
)


def migrate(connection, provenance_manifest=None):
    for sql in DDL:
        connection.exec_driver_sql(sql)
    additions={'inventory_batches': [('fifo_priority','INTEGER NOT NULL DEFAULT 1'),
                                    ('cost_source',"TEXT NOT NULL DEFAULT 'COST_UNVERIFIED'")],
        'inventory_unknown_cost': [('source_state',"TEXT NOT NULL DEFAULT 'MISSING'"),
            ('source_batch_id','INTEGER REFERENCES inventory_batches(id)'),
            ('source_segment_id','INTEGER REFERENCES inventory_provenance_segments(id)')],
        'inventory_cost_resolution': [('source_state','TEXT'),('source_batch_id','INTEGER'),
            ('actor','TEXT'),('evidence','TEXT'),('created_at','TEXT')],
        'orders': [('cost_status', "TEXT NOT NULL DEFAULT 'COST_UNVERIFIED'")],
        'order_items': [('cost_status', "TEXT NOT NULL DEFAULT 'COST_UNVERIFIED'"),
                        ('unknown_cost_quantity','INTEGER NOT NULL DEFAULT 0'),
                        ('fifo_details',"TEXT NOT NULL DEFAULT '[]'")]}
    for table,fields in additions.items():
        columns={c['name'] for c in inspect(connection).get_columns(table)}
        for name,definition in fields:
            if name not in columns:
                connection.exec_driver_sql('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
    # A reviewed manifest is explicit, immutable audit input, never a price
    connection.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS ix_unknown_cost_source_segment ON inventory_unknown_cost(source_segment_id)')
    connection.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_cost_provenance_batch_state ON inventory_provenance_segments(batch_id,source_state)')
    # guess. Identity, quantity and original cost must match before tagging.
    for record in provenance_manifest or []:
        b=connection.execute(text('SELECT * FROM inventory_batches WHERE id=:id'),
                             {'id':record['batch_id']}).mappings().one()
        for key in ('product_id','warehouse_id','quantity','remain_qty','unit_cost'):
            if b[key]!=record[key]: raise ValueError('来源审计与当前批次不一致，禁止迁移')
        if sum(s['quantity'] for s in record['segments'])!=b['remain_qty']:
            raise ValueError('来源分段数量不守恒')
        if record['classification'] not in UNTRUSTED[:2]:
            raise ValueError('历史审计不能自动升级为可信成本')
        connection.execute(text('UPDATE inventory_batches SET cost_source=:s WHERE id=:id'),
            {'s':record['classification'],'id':b['id']})
        for index,segment in enumerate(record['segments']):
            if segment['source_state'] not in UNTRUSTED[:2] or segment['quantity']<=0:
                raise ValueError('来源分段状态或数量无效')
            existing=connection.execute(text('SELECT source_state,quantity FROM inventory_provenance_segments WHERE batch_id=:b AND segment_index=:i'),{'b':b['id'],'i':index}).first()
            if existing and tuple(existing)!=(segment['source_state'],segment['quantity']):
                raise ValueError('已有来源分段与审计冲突')
            connection.execute(text('''INSERT OR IGNORE INTO inventory_provenance_segments
                (batch_id,segment_index,source_state,quantity,original_unit_cost,evidence,created_at)
                VALUES (:b,:i,:s,:q,:c,:e,CURRENT_TIMESTAMP)'''),
                {'b':b['id'],'i':index,'s':segment['source_state'],'q':segment['quantity'],
                 'c':b['unit_cost'],'e':record['evidence']})
    # Every unverified numeric lot gets a source segment. No numeric lot is
    # trusted simply because unit_cost is populated.
    connection.exec_driver_sql('''INSERT INTO inventory_provenance_segments
        (batch_id,segment_index,source_state,quantity,original_unit_cost,evidence,created_at)
        SELECT b.id,0,b.cost_source,b.remain_qty,b.unit_cost,'历史数字成本，未取得采购证明',CURRENT_TIMESTAMP
        FROM inventory_batches b WHERE b.remain_qty>0
        AND b.cost_source IN ('REFERENCE_PRICE_FALLBACK','COST_UNVERIFIED')
        AND NOT EXISTS(SELECT 1 FROM inventory_provenance_segments p WHERE p.batch_id=b.id)''')
    connection.exec_driver_sql('''INSERT INTO inventory_unknown_cost
        (product_id,warehouse_id,quantity,remain_qty,created_at,source_state,source_batch_id,source_segment_id)
        SELECT b.product_id,b.warehouse_id,p.quantity,p.quantity,CURRENT_TIMESTAMP,p.source_state,b.id,p.id
        FROM inventory_provenance_segments p JOIN inventory_batches b ON b.id=p.batch_id
        WHERE NOT EXISTS(SELECT 1 FROM inventory_unknown_cost u WHERE u.source_segment_id=p.id)''')
    # Physical gap excludes resolved audit lots; those raw historical rows
    # remain intact but no longer represent an effective available cost layer.
    connection.exec_driver_sql('''INSERT INTO inventory_unknown_cost
      (product_id,warehouse_id,quantity,remain_qty,created_at)
      SELECT s.product_id,s.warehouse_id,s.quantity-COALESCE(b.qty,0),
             s.quantity-COALESCE(b.qty,0),CURRENT_TIMESTAMP
      FROM warehouse_stock s LEFT JOIN
      (SELECT product_id,warehouse_id,SUM(qty) qty FROM
        (SELECT product_id,warehouse_id,remain_qty qty FROM inventory_batches
          WHERE cost_source IN ('VERIFIED_REAL_COST','RESTORED_VERIFIED_FIFO')
         UNION ALL SELECT product_id,warehouse_id,remain_qty qty FROM inventory_unknown_cost
          WHERE status='COST_INCOMPLETE') GROUP BY product_id,warehouse_id) b
      ON b.product_id=s.product_id AND b.warehouse_id=s.warehouse_id
      WHERE s.quantity>COALESCE(b.qty,0) AND NOT EXISTS
      (SELECT 1 FROM inventory_unknown_cost u WHERE u.product_id=s.product_id
       AND u.warehouse_id=s.warehouse_id AND u.source_state='MISSING')''')


def ready(session):
    return inspect(session.connection()).has_table('inventory_unknown_cost')


def capture(session, product_id, warehouse_id):
    """Capture pre-operation historical gap, never using reference prices."""
    from models import WarehouseStock, InventoryBatch
    if not ready(session):
        return []
    # Dynamically encountered legacy numeric lots also remain unverified.
    for batch in session.query(InventoryBatch).filter_by(product_id=product_id,warehouse_id=warehouse_id).all():
        if batch.cost_source in VERIFIED or int(batch.remain_qty or 0)<=0:
            continue
        segments=session.execute(text('SELECT * FROM inventory_provenance_segments WHERE batch_id=:b'),{'b':batch.id}).mappings().all()
        if not segments:
            session.execute(text('''INSERT INTO inventory_provenance_segments
                (batch_id,segment_index,source_state,quantity,original_unit_cost,evidence,created_at)
                VALUES (:b,0,:s,:q,:c,'未验证数字成本',:at)'''),
                {'b':batch.id,'s':batch.cost_source or 'COST_UNVERIFIED','q':batch.remain_qty,
                 'c':batch.unit_cost,'at':datetime.utcnow().isoformat()})
            segments=session.execute(text('SELECT * FROM inventory_provenance_segments WHERE batch_id=:b'),{'b':batch.id}).mappings().all()
        for segment in segments:
            session.execute(text('''INSERT INTO inventory_unknown_cost
                (product_id,warehouse_id,quantity,remain_qty,created_at,source_state,source_batch_id,source_segment_id)
                SELECT :p,:w,:q,:q,:at,:s,:b,:segment WHERE NOT EXISTS
                (SELECT 1 FROM inventory_unknown_cost WHERE source_segment_id=:segment)'''),
                {'p':product_id,'w':warehouse_id,'q':segment['quantity'],'s':segment['source_state'],
                 'b':batch.id,'segment':segment['id'],'at':datetime.utcnow().isoformat()})
    existing = session.execute(text('''SELECT * FROM inventory_unknown_cost
        WHERE product_id=:p AND warehouse_id=:w ORDER BY id'''),
        {'p':product_id,'w':warehouse_id}).mappings().all()
    if not any(r['source_state']=='MISSING' for r in existing):
        stock = session.query(WarehouseStock).filter_by(product_id=product_id,warehouse_id=warehouse_id).first()
        known = sum(int(b.remain_qty or 0) for b in session.query(InventoryBatch).filter_by(
            product_id=product_id,warehouse_id=warehouse_id).all() if b.cost_source in VERIFIED)
        represented=known+sum(r['remain_qty'] for r in existing if r['status']==INCOMPLETE)
        missing = max(0,int(stock.quantity or 0)-represented) if stock else 0
        # Remember a zero gap as complete; repeated consumption in the same
        # transaction must not reclassify consumed real lots as unknown history.
        session.execute(text('''INSERT INTO inventory_unknown_cost
            (product_id,warehouse_id,quantity,remain_qty,created_at,status)
            VALUES (:p,:w,:q,:q,:at,:status)'''),
            {'p':product_id,'w':warehouse_id,'q':missing,'at':datetime.utcnow().isoformat(),
             'status':INCOMPLETE if missing else 'COST_COMPLETE'})
        existing = session.execute(text('''SELECT * FROM inventory_unknown_cost
            WHERE product_id=:p AND warehouse_id=:w ORDER BY id'''),
            {'p':product_id,'w':warehouse_id}).mappings().all()
    return existing


def consume_unknown(session, product_id, warehouse_id, need):
    details=[]
    for layer in capture(session,product_id,warehouse_id):
        if layer['status'] != INCOMPLETE or need <= 0:
            continue
        take=min(need,layer['remain_qty'])
        if take:
            session.execute(text('UPDATE inventory_unknown_cost SET remain_qty=remain_qty-:q WHERE id=:id'),
                            {'q':take,'id':layer['id']})
            details.append(dict(batch_id=None,batch_no='HISTORICAL_UNKNOWN',qty=take,
                unit_cost=None,unknown_layer_id=layer['id'],
                cost_source=layer['source_state'],source_batch_id=layer['source_batch_id'],
                source_segment_id=layer['source_segment_id'],
                offset=layer['quantity']-layer['remain_qty']))
            need-=take
    return need,details


def resolve_details(session, details):
    """Translate an original unknown consumption into its administrator-proven lots."""
    from models import InventoryBatch
    result=[]
    for d in details:
        if d.get('unknown_layer_id'):
            start,end=d['offset'],d['offset']+d['qty']
            rows=session.execute(text('''SELECT * FROM inventory_cost_resolution
                WHERE layer_id=:id ORDER BY start_offset'''),{'id':d['unknown_layer_id']}).mappings()
            count=0
            for row in rows:
                qty=max(0,min(end,row['start_offset']+row['quantity'])-max(start,row['start_offset']))
                if qty:
                    batch=session.get(InventoryBatch,row['batch_id'])
                    if not batch or batch.cost_source not in VERIFIED: raise ValueError(BLOCKED)
                    result.append(dict(batch_id=batch.id,qty=qty,unit_cost=batch.unit_cost,
                                       cost_source=batch.cost_source))
                    count+=qty
            if count != d['qty']: raise ValueError(BLOCKED)
        elif not d.get('batch_id') or d.get('unit_cost') is None:
            raise ValueError(BLOCKED)
        else:
            batch=session.get(InventoryBatch,d['batch_id'])
            if not batch or batch.cost_source not in VERIFIED:
                raise ValueError(BLOCKED)
            result.append(d)
    return result


def is_unknown(details):
    return any(d.get('unit_cost') is None for d in details)


def take_details(pool, quantity):
    """Partition consumption provenance exactly across automatic order packages."""
    result=[]
    while quantity:
        if not pool: raise ValueError('FIFO成本来源数量不足')
        d=pool[0]; qty=min(quantity,d['qty']); part=dict(d,qty=qty)
        result.append(part); quantity-=qty
        if qty==d['qty']: pool.pop(0)
        else:
            d['qty']-=qty
            if 'offset' in d: d['offset']+=qty
    return result


def order_summary(session, order):
    """Read-only cost report; no recalculation/writes to historical monetary rows."""
    cost=profit=0.0
    known_quantity=missing_quantity=0
    legacy_unverified=False
    for item in order.items:
        if getattr(item,'cost_status','COST_UNVERIFIED')=='COST_UNVERIFIED':
            legacy_unverified=True
            missing_quantity+=item.quantity
            continue
        try:
            details=json.loads(getattr(item,'fifo_details','[]') or '[]')
            if details:
                if sum(d['qty'] for d in details)!=item.quantity: raise ValueError(BLOCKED)
                if any(d.get('cost_source')=='BUNDLE_INDEPENDENT' for d in details):
                    from bundle_inventory import item_cost
                    amount=item_cost(session,item)
                    known_quantity+=item.quantity
                    cost+=amount
                    profit+=item.quantity*item.unit_price-amount
                    continue
                amount=0.0
                for detail in details:
                    try:
                        resolved=resolve_details(session,[detail])
                    except (ValueError,TypeError,KeyError):
                        missing_quantity+=detail['qty']
                        continue
                    amount+=sum(d['qty']*d['unit_cost'] for d in resolved)
                    known_quantity+=detail['qty']
            elif item.cost_status=='COST_COMPLETE':
                amount=float(item.cost_amount or 0)
                known_quantity+=item.quantity
            else: raise ValueError(BLOCKED)
        except (ValueError,TypeError,KeyError):
            missing_quantity+=item.quantity
            continue
        cost+=amount
        profit+=item.quantity*item.unit_price-amount
    incomplete=missing_quantity>0
    return dict(cost_status=('COST_UNVERIFIED' if legacy_unverified else INCOMPLETE) if incomplete else 'COST_COMPLETE',
                cost_amount=None if incomplete else round(cost,2),
                total_profit=None if incomplete else round(profit,2),
                known_cost_amount=round(cost,2),known_cost_quantity=known_quantity,
                historical_missing_quantity=missing_quantity,
                cost_label='历史进货价缺失' if incomplete else '销售成本已记录')


def item_summary(session, item):
    from types import SimpleNamespace
    data=order_summary(session,SimpleNamespace(items=[item]))
    data['profit']=data.pop('total_profit')
    data['unit_cost']=None if data['cost_amount'] is None else data['cost_amount']/item.quantity
    return data


def listing(session):
    """Read-only status includes in-stock and consumed-but-unresolved quantities."""
    from models import WarehouseStock, InventoryBatch
    rows=[]
    layers=[]
    if ready(session):
        layers=session.execute(text("SELECT * FROM inventory_unknown_cost WHERE status='COST_INCOMPLETE' ORDER BY id")).mappings().all()
    for stock in session.query(WarehouseStock).all():
        key=(stock.product_id,stock.warehouse_id)
        known=sum(int(b.remain_qty or 0) for b in session.query(InventoryBatch).filter_by(
            product_id=stock.product_id,warehouse_id=stock.warehouse_id).all() if b.cost_source in VERIFIED)
        gap=max(0,int(stock.quantity or 0)-known)
        selected=[r for r in layers if (r['product_id'],r['warehouse_id'])==key]
        if not gap and not selected: continue
        p=stock.product
        for layer in selected or [None]:
            qty=layer['quantity'] if layer else gap
            remain=layer['remain_qty'] if layer else gap
            rows.append(dict(layer_id=layer['id'] if layer else None,product_id=stock.product_id,
                product_name=p.name,warehouse_id=stock.warehouse_id,warehouse_name=stock.warehouse.name,
                category_id=p.parent_id,category_name=p.parent.name if p.parent else '',
                physical_quantity=stock.quantity,unknown_on_hand=remain,
                unknown_consumed=qty-remain,ledger_unknown_on_hand=remain,
                quantity_aligned=gap==sum(r['remain_qty'] for r in selected) if selected else True,
                required_quantity=qty,status=INCOMPLETE,source_state=layer['source_state'] if layer else 'MISSING',
                source_batch_id=layer['source_batch_id'] if layer else None,
                source_segment_id=layer['source_segment_id'] if layer else None,
                label='历史成本待补录',reference_cost=p.cost_price))
    return rows


def baseline(session):
    """Effective quantities, not raw historical numeric lots. Read-only."""
    from models import WarehouseStock, InventoryBatch
    result=[]
    for stock in session.query(WarehouseStock).order_by(WarehouseStock.id):
        verified=sum(int(b.remain_qty or 0) for b in session.query(InventoryBatch).filter_by(
            product_id=stock.product_id,warehouse_id=stock.warehouse_id) if b.cost_source in VERIFIED)
        amounts={state:0 for state in UNTRUSTED}
        if ready(session):
            layers=session.execute(text('''SELECT source_state,remain_qty FROM inventory_unknown_cost
                WHERE product_id=:p AND warehouse_id=:w AND status='COST_INCOMPLETE' '''),
                {'p':stock.product_id,'w':stock.warehouse_id}).all()
            for state,quantity in layers:
                if state not in amounts: raise ValueError('未知成本来源状态')
                amounts[state]+=quantity
        else:
            for batch in session.query(InventoryBatch).filter_by(product_id=stock.product_id,warehouse_id=stock.warehouse_id):
                if batch.cost_source not in VERIFIED:
                    amounts['COST_UNVERIFIED']+=int(batch.remain_qty or 0)
            amounts['MISSING']=int(stock.quantity or 0)-verified-sum(amounts.values())
        row=dict(warehouse_id=stock.warehouse_id,warehouse=stock.warehouse.name,
            category_id=stock.product.parent_id,category=stock.product.parent.name if stock.product.parent else '',
            product_id=stock.product_id,product=stock.product.name,physical_qty=int(stock.quantity or 0),
            verified_cost_qty=verified,fallback_qty=amounts['REFERENCE_PRICE_FALLBACK'],
            unverified_qty=amounts['COST_UNVERIFIED'],missing_cost_qty=amounts['MISSING'])
        row['conserved']=row['physical_qty']==sum(row[k] for k in ('verified_cost_qty','fallback_qty','unverified_qty','missing_cost_qty')) and min(amounts.values())>=0
        result.append(row)
    return result


def supplement(database, warehouse_id, category_id, product_id, batches, actor, evidence, layer_id=None):
    from sqlalchemy.orm import Session
    from models import Product, InventoryBatch
    if not isinstance(evidence,str) or not evidence.strip() or len(evidence)>500:
        raise ValueError('请填写真实成本凭据说明（不保存凭证密钥）')
    if not isinstance(batches,list) or not 1<=len(batches)<=100:
        raise ValueError('请填写1–100个真实历史成本批次')
    checked=[]
    for b in batches:
        if not isinstance(b,dict): raise ValueError('历史成本批次格式无效')
        qty=b.get('quantity'); cost=b.get('unit_cost')
        if isinstance(qty,bool) or not isinstance(qty,int) or qty<=0:
            raise ValueError('批次数量必须为正整数')
        if isinstance(cost,bool) or not isinstance(cost,(int,float)) or not math.isfinite(cost) or cost<0:
            raise ValueError('实际单位成本必须是有限非负数')
        checked.append((qty,float(cost)))
    with database.engine.connect() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE')
        s=Session(bind=c,autoflush=False)
        try:
            if not ready(s): raise ValueError('成本Migration尚未执行')
            p=s.get(Product,product_id)
            if not p or p.is_parent or p.parent_id!=category_id or p.default_warehouse_id!=warehouse_id:
                raise ValueError('仓库、分类和商品不匹配')
            layers=[r for r in capture(s,product_id,warehouse_id) if r['status']==INCOMPLETE]
            all_layers=layers
            if layer_id is not None:
                layers=[r for r in layers if r['id']==layer_id]
            if len(layers)!=1: raise ValueError('没有可补录的历史成本层，或需要人工核查')
            layer=layers[0]
            from models import WarehouseStock
            stock=s.query(WarehouseStock).filter_by(product_id=product_id,warehouse_id=warehouse_id).one()
            known=sum(int(b.remain_qty or 0) for b in s.query(InventoryBatch).filter_by(
                product_id=product_id,warehouse_id=warehouse_id).all() if b.cost_source in VERIFIED)
            if int(stock.quantity or 0)!=known+sum(r['remain_qty'] for r in all_layers):
                raise ValueError('库存数量与成本层不守恒，禁止确认成本完整，请先核查')
            if sum(q for q,_ in checked)!=layer['quantity']:
                raise ValueError('历史成本批次数量之和必须等于当前待补录数量')
            consumed=layer['quantity']-layer['remain_qty']
            offset=0
            for qty,cost in checked:
                used=max(0,min(offset+qty,consumed)-offset)
                batch=InventoryBatch(product_id=product_id,warehouse_id=warehouse_id,
                    batch_no='HIST-'+str(layer['id'])+'-'+str(offset),quantity=qty,
                    remain_qty=qty-used,unit_cost=cost,fifo_priority=0,
                    cost_source='VERIFIED_REAL_COST',
                    note='管理员真实历史成本补录：'+evidence)
                s.add(batch); s.flush()
                s.execute(text('''INSERT INTO inventory_cost_resolution
                    (layer_id,batch_id,start_offset,quantity,source_state,source_batch_id,actor,evidence,created_at)
                    VALUES (:l,:b,:o,:q,:state,:source,:actor,:evidence,:at)'''),
                    {'l':layer['id'],'b':batch.id,'o':offset,'q':qty,'state':layer['source_state'],
                     'source':layer['source_batch_id'],'actor':actor,'evidence':evidence,
                     'at':datetime.utcnow().isoformat()})
                offset+=qty
            s.execute(text('''UPDATE inventory_unknown_cost SET status='COST_COMPLETE',
                remain_qty=0,resolved_at=:at,resolved_by=:actor,evidence=:e WHERE id=:id'''),
                {'at':datetime.utcnow().isoformat(),'actor':actor,'e':evidence,'id':layer['id']})
            c.commit()
            remaining_layers=[r for r in all_layers if r['id']!=layer['id']]
            return dict(status=INCOMPLETE if remaining_layers else 'COST_COMPLETE',
                        layer_status='COST_COMPLETE',quantity=offset,stock_changed=False)
        except Exception:
            c.rollback(); raise
        finally:
            s.close()
