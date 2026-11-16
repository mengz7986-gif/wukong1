"""Bundle definitions and complete-portion fulfillment plans.

The order transaction must call plan() while holding its existing write lock.
This module never consumes stock or FIFO; its output is real fulfillment rows.
"""
import json
import random
import unicodedata
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import text


TABLES = ('bundle_definitions', 'bundle_names', 'bundle_user_prices',
          'bundle_sales', 'bundle_fulfillment', 'bundle_settings')


def migrate(connection):
    """Additive migration only. Existing business tables are not altered."""
    statements = [
        '''CREATE TABLE IF NOT EXISTS bundle_definitions (
        id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES products(id),
        name TEXT NOT NULL, config TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
        deleted INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS bundle_names (
        normalized_name TEXT PRIMARY KEY, display_name TEXT NOT NULL,
        bundle_id INTEGER NOT NULL REFERENCES bundle_definitions(id),
        kind TEXT NOT NULL CHECK(kind IN ('standard','alias')))''',
        '''CREATE TABLE IF NOT EXISTS bundle_user_prices (
        bundle_id INTEGER NOT NULL REFERENCES bundle_definitions(id),
        user_id INTEGER NOT NULL REFERENCES users(id), price TEXT NOT NULL,
        PRIMARY KEY(bundle_id,user_id))''',
        '''CREATE TABLE IF NOT EXISTS bundle_sales (
        id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
        bundle_id INTEGER NOT NULL REFERENCES bundle_definitions(id),
        instance_key TEXT NOT NULL, snapshot TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK(quantity>0), amount TEXT NOT NULL,
        UNIQUE(order_id,instance_key))''',
        '''CREATE TABLE IF NOT EXISTS bundle_fulfillment (
        sale_id INTEGER NOT NULL REFERENCES bundle_sales(id),
        order_item_id INTEGER NOT NULL UNIQUE REFERENCES order_items(id),
        PRIMARY KEY(sale_id,order_item_id))''',
        '''CREATE TABLE IF NOT EXISTS bundle_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), max_portions INTEGER NOT NULL CHECK(max_portions>0),
        shipping_mode TEXT NOT NULL DEFAULT 'free' CHECK(shipping_mode='free'))''',
    ]
    for sql in statements:
        connection.execute(text(sql))
    connection.execute(text("INSERT OR IGNORE INTO bundle_settings(id,max_portions) VALUES (1,1)"))
    import bundle_inventory
    bundle_inventory.migrate(connection)


def rollback(connection):
    """Safe rollback is allowed only before any bundle sales have been committed."""
    if connection.execute(text('SELECT COUNT(*) FROM bundle_sales')).scalar():
        raise ValueError('已有福袋成交，禁止删除销售快照；请回退代码并保留福袋数据')
    import bundle_inventory
    if connection.execute(text('SELECT COUNT(*) FROM bundle_inventory_movements')).scalar():
        raise ValueError('已有福袋库存流水，禁止删除库存审计记录')
    for name in reversed(bundle_inventory.TABLES):
        connection.execute(text('DROP TABLE IF EXISTS '+name))
    for name in reversed(TABLES):
        connection.execute(text('DROP TABLE IF EXISTS '+name))


def normalized(value):
    value = unicodedata.normalize('NFKC', str(value)).strip().casefold()
    return ''.join(value.split())


def positive(value, label):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError(label+'须为正整数')
    return int(value)


def price(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError()
        return str(number.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
    except Exception:
        raise ValueError('售价须为有效非负金额') from None


def validate_config(category, config, products, package_capacity=None, inventory_mode='components'):
    """Resolve real IDs, never guess a category or flavor from display text."""
    if not category or not category.is_parent or category.is_archived or not category.is_active:
        raise ValueError('所属分类不存在或已停用')
    mode = config.get('mode')
    if mode not in ('fixed', 'random', 'custom'):
        raise ValueError('请选择固定、随机或客户自选配货方式')
    units = positive(config.get('units'), '每份总支数')
    if package_capacity is not None and units > positive(package_capacity, '包裹容量'):
        raise ValueError('单个福袋支数超过包裹容量，不能拆散福袋')
    components = config.get('components', [])
    pool = config.get('pool', [])
    if mode == 'fixed':
        if not components and inventory_mode!='independent':
            raise ValueError('固定福袋须配置组成口味')
        ids = [positive(r.get('product_id'), '口味ID') for r in components]
        quantities = [positive(r.get('quantity'), '口味数量') for r in components]
        if len(set(ids)) != len(ids):
            raise ValueError('组成口味重复，请合并数量')
        if components and sum(quantities) != units:
            raise ValueError('组成数量合计必须等于福袋总支数')
    else:
        ids = [positive(i, '候选口味ID') for i in pool]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError('请选择不重复的候选口味池')
    for id in ids:
        p = products.get(id)
        if not p or p.parent_id != category.id or p.is_parent or p.is_archived or not p.is_active:
            raise ValueError('组成口味失效或不属于当前分类：'+str(id))
    prices = config.get('prices', {})
    if 'regular' not in prices:
        raise ValueError('请填写福袋销售价格')
    prices = {k: price(v) for k,v in prices.items() if k in ('regular','level1','level2')}
    maximum = positive(config.get('max_per_flavor', units), '单口味最多支数')
    allow = config.get('allow_repeat', True)
    if not isinstance(allow,bool):
        raise ValueError('是否允许重复须为布尔值')
    if mode == 'random' and units > len(ids)*(maximum if allow else 1):
        raise ValueError('候选池不能满足当前不同口味/数量配置')
    return dict(mode=mode, units=units, components=components if mode=='fixed' else [],
                pool=ids if mode!='fixed' else [], prices=prices,
                allow_repeat=allow,max_per_flavor=maximum,
                show_flavors=bool(config.get('show_flavors',False)))


def save(session, category, name, config, products, aliases=(), bundle_id=None,
         enabled=False, package_capacity=None, reserved_names=(), inventory_mode=None):
    """Caller owns the transaction. Reserve names for disabled bundles too."""
    name = str(name).strip()
    names = [name]+[str(a).strip() for a in aliases]
    if any(not 1<=len(n)<=100 or any(ord(c)<32 for c in n) for n in names):
        raise ValueError('福袋名称/叫法不能为空或含控制字符')
    keys = [normalized(n) for n in names]
    if len(set(keys)) != len(keys):
        raise ValueError('福袋名称与叫法重复')
    reserved = {normalized(n) for n in reserved_names}
    if reserved.intersection(keys):
        raise ValueError('福袋名称/叫法与现有商品或叫法冲突')
    if inventory_mode is None:
        import bundle_inventory
        inventory_mode=bundle_inventory.view(session,bundle_id)['mode'] if bundle_id else 'components'
    config = validate_config(category, config, products, package_capacity,inventory_mode)
    if bundle_id is not None:
        old = session.execute(text('SELECT * FROM bundle_definitions WHERE id=:id AND deleted=0'),{'id':bundle_id}).mappings().first()
        if not old or old['category_id']!=category.id:
            raise ValueError('福袋不存在或分类归属不一致')
    for key in keys:
        hit=session.execute(text('SELECT bundle_id,display_name FROM bundle_names WHERE normalized_name=:key'),{'key':key}).mappings().first()
        if hit and hit['bundle_id']!=bundle_id:
            raise ValueError('福袋名称/叫法冲突：'+hit['display_name'])
    body=json.dumps(config,ensure_ascii=False,sort_keys=True)
    if bundle_id is None:
        result=session.execute(text('INSERT INTO bundle_definitions(category_id,name,config,enabled) VALUES (:category,:name,:config,:enabled)'),dict(category=category.id,name=name,config=body,enabled=int(enabled)))
        bundle_id=result.lastrowid
    else:
        session.execute(text('UPDATE bundle_definitions SET name=:name,config=:config,enabled=:enabled,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=:id'),dict(name=name,config=body,enabled=int(enabled),id=bundle_id))
        session.execute(text('DELETE FROM bundle_names WHERE bundle_id=:id'),{'id':bundle_id})
    for index,(key,display) in enumerate(zip(keys,names)):
        session.execute(text('INSERT INTO bundle_names(normalized_name,display_name,bundle_id,kind) VALUES (:key,:display,:id,:kind)'),dict(key=key,display=display,id=bundle_id,kind='standard' if index==0 else 'alias'))
    return bundle_id


def lookup(session, name):
    row=session.execute(text('SELECT b.* FROM bundle_names n JOIN bundle_definitions b ON b.id=n.bundle_id WHERE n.normalized_name=:name AND b.enabled=1 AND b.deleted=0'),{'name':normalized(name)}).mappings().first()
    import rule_dictionary
    stored=rule_dictionary.runtime_store()
    if row:
        overrides=[r for r in stored.get('aliases',[]) if r.get('type')=='BUNDLE_ALIASES'
            and r.get('bundle_id')==row['id'] and normalized(r.get('legacy_alias',''))==normalized(name)]
        if overrides and not any(r.get('status')=='active' and normalized(r['alias'])==normalized(name) for r in overrides):
            row=None
    if row is None:
        ids={r['bundle_id'] for r in stored.get('aliases',[]) if r.get('type')=='BUNDLE_ALIASES'
             and r.get('status')=='active' and normalized(r['alias'])==normalized(name)}
        if len(ids)>1: raise ValueError('福袋叫法存在冲突，请管理员审核')
        if ids:
            row=session.execute(text('SELECT * FROM bundle_definitions WHERE id=:id AND enabled=1 AND deleted=0'),{'id':next(iter(ids))}).mappings().first()
    return dict(row,config=json.loads(row['config'])) if row else None


def selling_price(session,bundle,user):
    custom=session.execute(text('SELECT price FROM bundle_user_prices WHERE bundle_id=:b AND user_id=:u'),{'b':bundle['id'],'u':user.id}).scalar()
    if custom is not None:
        return price(custom)
    # Same tier mapping as Product.get_price_for_agent_level.
    level={1:'level2',2:'level1'}.get(user.agent_level,'regular')
    return bundle['config']['prices'].get(level,bundle['config']['prices']['regular'])


def portion(config, available, selection=None, rng=None):
    """One complete portion. Never repeat against the configured policy."""
    units=config['units']; mode=config['mode']
    if mode=='fixed':
        result=Counter({int(r['product_id']):int(r['quantity']) for r in config['components']})
    elif mode=='custom':
        result=Counter()
        for row in selection or []:
            id=positive(row.get('product_id'),'自选口味ID')
            if id not in config['pool']:
                raise ValueError('自选口味不属于当前福袋口味池')
            result[id]+=positive(row.get('quantity'),'自选数量')
        if sum(result.values())!=units:
            raise ValueError('客户自选数量合计必须等于福袋总支数')
        if any(n>config['max_per_flavor'] or (not config['allow_repeat'] and n>1) for n in result.values()):
            raise ValueError('客户自选超过口味重复限制')
    else:
        rng=rng or random.SystemRandom();result=Counter()
        caps={id:min(int(available.get(id,0)),config['max_per_flavor'] if config['allow_repeat'] else 1) for id in config['pool']}
        if sum(caps.values())<units:
            raise ValueError('福袋可履约口味/库存不足，不能偷偷重复')
        for _ in range(units):
            id=rng.choice(sorted(id for id,n in caps.items() if n>result[id]))
            result[id]+=1
    if any(available.get(id,0)<n for id,n in result.items()):
        raise ValueError('福袋组成商品库存不足，整份未成交')
    return result


def plan_portions(bundle, count, warehouses, user_price, selections=None, rng=None):
    """Each warehouse maps source real product IDs to its actual stock identity.

    Mapping must come from existing confirmed logical identities, NOT fuzzy names.
    Work on a private availability copy so multiple portions cannot oversell.
    Caller feeds only active, permitted warehouses/products with sellable stock.
    """
    count=positive(count,'福袋份数')
    remaining={w:{id:dict(row) for id,row in products.items()} for w,products in warehouses.items()}
    output=[]
    for index in range(count):
        options=[]
        for warehouse,products in remaining.items():
            try:
                components=portion(bundle['config'],{id:r['quantity'] for id,r in products.items()},
                    (selections or {}).get(index),rng)
            except ValueError:
                continue
            options.append((warehouse,components))
        if not options:
            raise ValueError('没有单一仓库能完整履约福袋，整单未成交')
        # Existing caller ordering implements stock/explicit warehouse/balancing.
        warehouse,components=options[0]
        items=[]
        for id,n in sorted(components.items()):
            row=remaining[warehouse][id];row['quantity']-=n
            items.append(dict(product_id=row['product_id'],warehouse_id=warehouse,quantity=n))
        output.append(dict(bundle_id=bundle['id'],instance_key=str(index),quantity=1,
            amount=price(user_price),shipping_fee='0.00',warehouse_id=warehouse,
            snapshot=json.loads(json.dumps(bundle,ensure_ascii=False)),items=items))
    return output


def allocate(session, requests, user, ordinary_items=(), explicit_warehouse_id=None):
    """Plan inside the existing order transaction, using its router unchanged."""
    from models import Product, WarehouseStock
    from warehouse_allocation import select_warehouse
    import config
    prepared=[];sales={};reserved=Counter()
    for row in ordinary_items:
        if row.get('warehouse_id'):
            reserved[(int(row['warehouse_id']),int(row['product_id']))]+=int(row['quantity'])
    for request_index,request in enumerate(requests):
        id=positive(request.get('bundle_id'),'福袋ID')
        stored=session.execute(text('SELECT * FROM bundle_definitions WHERE id=:id AND enabled=1 AND deleted=0'),{'id':id}).mappings().first()
        if not stored:
            raise ValueError('福袋不存在或已停用')
        bundle=dict(stored,config=json.loads(stored['config']))
        category=session.get(Product,bundle['category_id'])
        source={p.id:p for p in session.query(Product).filter_by(parent_id=category.id).all()} if category else {}
        # Physical package capacity is not the ordinary category split setting.
        capacity=int(getattr(config,'UNITS_PER_PACKAGE',0) or 0) or None
        import bundle_inventory
        inventory=bundle_inventory.view(session,id)
        bundle['config']=validate_config(category,bundle['config'],source,capacity,inventory['mode'])
        bundle['category_name']=category.name
        units=positive(request.get('quantity'),'福袋份数')
        selected=request.get('selections') or {}
        selected={int(k):v for k,v in selected.items()}
        amount=selling_price(session,bundle,user)
        import bundle_inventory
        inventory=bundle_inventory.view(session,id)
        bundle['inventory_mode']=inventory['mode']
        if inventory['mode']=='independent':
            # Independent portions are physically bound to their owning category's
            # warehouse. Never silently move their balance across warehouses.
            from models import Warehouse
            import sales_pause
            warehouse=session.get(Warehouse,category.default_warehouse_id)
            if not warehouse or not warehouse.is_active or (explicit_warehouse_id is not None and warehouse.id!=int(explicit_warehouse_id)):
                raise ValueError('独立福袋所属仓库不可用或与指定仓库不一致')
            if sales_pause.is_paused(getattr(config,'SALES_PAUSE_SECTION',None),warehouse.id):raise ValueError('所属仓库暂停接单')
            private=getattr(config,'WHOLESALE_PRIVATE_WAREHOUSE_NAME','')
            owner=str(getattr(config,'WHOLESALE_PRIVATE_USERNAME','')).lstrip('@').casefold()
            if private and warehouse.name==private and str(user.username or '').lstrip('@').casefold()!=owner:raise ValueError('无权使用所属仓库')
            used=reserved[('bundle',id)]
            if inventory['remaining']-used<units:raise ValueError('福袋独立库存不足，整单未成交')
            available={p:bundle['config']['units'] for p in source}
            for portion_index in range(units):
                composition=portion(bundle['config'],available,selected.get(portion_index))
                key=str(request_index)+':'+str(portion_index)
                per,remainder=divmod(int(Decimal(amount)*100),bundle['config']['units'])
                if not composition:
                    # The owning category is an accounting carrier, not an invented flavor.
                    for unit in range(bundle['config']['units']):
                        prepared.append(dict(product_id=category.id,warehouse_id=warehouse.id,
                            quantity=1,bundle_instance=key,
                            bundle_unit_price=(per+int(unit<remainder))/100,
                            independent_bundle_id=id))
                for pid,n in sorted(composition.items()):
                    if source[pid].default_warehouse_id!=warehouse.id:raise ValueError('独立福袋口味所属仓库不一致')
                    for _ in range(n):
                        extra=int(remainder>0);remainder-=extra
                        prepared.append(dict(product_id=pid,warehouse_id=warehouse.id,quantity=1,
                            bundle_instance=key,bundle_unit_price=(per+extra)/100,independent_bundle_id=id))
                sales[key]=dict(bundle_id=id,quantity=1,amount=amount,instance_key=key,snapshot=json.loads(json.dumps(bundle,ensure_ascii=False)))
            reserved[('bundle',id)]+=units
            continue
        for portion_index in range(units):
            # Exact GLOBAL identities are those already used by the router.
            candidate_ids={p.default_warehouse_id for p in session.query(Product).filter_by(
                is_parent=True,name=category.name,is_active=True,is_archived=False)}
            if explicit_warehouse_id is not None:
                candidate_ids &= {int(explicit_warehouse_id)}
            candidates=[]
            for warehouse in sorted(candidate_ids):
                available={}
                mapped={}
                for source_id,p in source.items():
                    matches=[r for r in session.query(Product).filter_by(name=p.name,
                        default_warehouse_id=warehouse,is_parent=False,is_active=True,is_archived=False)
                        if r.parent and r.parent.name==category.name and r.parent.is_active and not r.parent.is_archived]
                    if len(matches)==1:
                        r=matches[0];stock=session.query(WarehouseStock).filter_by(product_id=r.id,warehouse_id=warehouse).first()
                        from stock_rules import sellable_quantity
                        available[source_id]=0 if r.auto_unlisted else max(0,sellable_quantity(
                            int(stock.quantity or 0) if stock else 0,r.low_stock_threshold,
                            getattr(config,'STOCK_AUTO_UNLIST_MODE','threshold'))-reserved[(warehouse,r.id)])
                        mapped[source_id]=r.id
                try:
                    composition=portion(bundle['config'],available,selected.get(portion_index))
                    items=[dict(product_id=mapped[p],warehouse_id=warehouse,quantity=n) for p,n in composition.items()]
                    # Validates disabled/private/paused warehouses and rotates ties.
                    select_warehouse(session,items,user,explicit_warehouse_id=warehouse)
                    candidates.append(items)
                except ValueError:
                    continue
            if not candidates:
                raise ValueError('没有单一仓库能完整履约福袋，整单未成交')
            if bundle['config']['mode']=='fixed' or bundle['config']['mode']=='custom':
                chosen,_,_=select_warehouse(session,candidates[0],user,
                    explicit_warehouse_id=explicit_warehouse_id)
                allowed={rows[0]['warehouse_id']:rows for rows in candidates}
                chosen=allowed.get(chosen[0]['warehouse_id'],candidates[0])
            else:
                # The same router balances candidate warehouses; random composition
                # must stay tied to the warehouse whose availability created it.
                chosen=candidates[0]
                for rows in candidates:
                    routed,_,_=select_warehouse(session,rows,user,explicit_warehouse_id=explicit_warehouse_id)
                    if routed[0]['warehouse_id']==rows[0]['warehouse_id']:
                        chosen=rows;break
            key=str(request_index)+':'+str(portion_index)
            # Allocate sale revenue in cents to fulfillment units. This is revenue
            # attribution, never another cost price, and sums exactly to the sale.
            cents=int(Decimal(amount)*100);total=bundle['config']['units']
            per,remainder=divmod(cents,total)
            for item in chosen:
                reserved[(item['warehouse_id'],item['product_id'])]+=item['quantity']
                for unit in range(item['quantity']):
                    extra=1 if remainder>0 else 0;remainder-=extra
                    prepared.append(dict(item,quantity=1,bundle_instance=key,
                        bundle_unit_price=(per+extra)/100))
            sales[key]=dict(bundle_id=id,quantity=1,amount=amount,
                instance_key=key,snapshot=json.loads(json.dumps(bundle,ensure_ascii=False)))
    return prepared,sales


def full_plan(session,requests,user,ordinary,explicit_warehouse_id=None):
    """Prefer a complete single warehouse for mixed sales, then existing routes.

    Dry plans only; neither failed probes nor random probes change business data.
    """
    from models import Warehouse
    from warehouse_allocation import select_warehouse
    import bundle_inventory
    if any(bundle_inventory.view(session,positive(r.get('bundle_id'),'福袋ID'))['mode']=='independent' for r in requests):
        mapped=select_warehouse(session,ordinary,user,cross_warehouse_confirmed=True,explicit_warehouse_id=explicit_warehouse_id)[0] if ordinary else []
        expanded,sales=allocate(session,requests,user,mapped,explicit_warehouse_id)
        return mapped,expanded,sales
    candidates=[]
    ids=[w.id for w in session.query(Warehouse).filter_by(is_active=True).order_by(Warehouse.id)]
    if explicit_warehouse_id is not None:ids=[int(explicit_warehouse_id)]
    for warehouse in ids:
        try:
            mapped=select_warehouse(session,ordinary,user,explicit_warehouse_id=warehouse)[0] if ordinary else []
            expanded,sales=allocate(session,requests,user,mapped,warehouse)
            candidates.append((warehouse,mapped,expanded,sales))
        except ValueError:
            continue
    if candidates:
        warehouse,mapped,expanded,sales=candidates[0]
        merged=Counter()
        for row in mapped+expanded:merged[(row['product_id'],row['warehouse_id'])]+=int(row['quantity'])
        chosen,_,_=select_warehouse(session,[dict(product_id=p,warehouse_id=w,quantity=n) for (p,w),n in merged.items()],user,
            explicit_warehouse_id=explicit_warehouse_id)
        best=next((r for r in candidates if r[0]==chosen[0]['warehouse_id']),candidates[0])
        return best[1],best[2],best[3]
    mapped=select_warehouse(session,ordinary,user,cross_warehouse_confirmed=True,
        explicit_warehouse_id=explicit_warehouse_id)[0] if ordinary else []
    expanded,sales=allocate(session,requests,user,mapped,explicit_warehouse_id)
    return mapped,expanded,sales


def packages(prepared, sales, ordinary_packages, max_portions):
    """Complete portions only, ordinary packages are passed through unmodified."""
    cap=positive(max_portions,'每笔限制福袋数量');groups={}
    for row in prepared:
        if row.get('bundle_instance') is not None:
            key=(row['warehouse_id'],row['bundle_instance'])
            groups.setdefault(key,[]).append(row)
    by_warehouse={}
    for (warehouse,key),rows in groups.items():
        by_warehouse.setdefault(warehouse,[]).append(rows)
    result=list(ordinary_packages)
    for portions in by_warehouse.values():
        for start in range(0,len(portions),cap):
            result.append([r for portion in portions[start:start+cap] for r in portion])
    return result


def save_sales(session,order,rows,sales):
    keys={row['bundle_instance'] for row in rows if row.get('bundle_instance') is not None}
    for key in sorted(keys):
        sale=sales[key]
        result=session.execute(text('INSERT INTO bundle_sales(order_id,bundle_id,instance_key,snapshot,quantity,amount) VALUES (:order,:bundle,:key,:snapshot,:qty,:amount)'),dict(order=order.id,bundle=sale['bundle_id'],key=key,snapshot=json.dumps(sale['snapshot'],ensure_ascii=False),qty=1,amount=sale['amount']))
        if sale.get('inventory_movement_id') is not None:
            session.execute(text('INSERT INTO bundle_inventory_sale_links VALUES (:s,:m)'),{'s':result.lastrowid,'m':sale['inventory_movement_id']})
        for row in rows:
            if row.get('bundle_instance')==key:
                session.execute(text('INSERT INTO bundle_fulfillment(sale_id,order_item_id) VALUES (:sale,:item)'),{'sale':result.lastrowid,'item':row['persisted_item_id']})


def receipt_sales(session,order_ids):
    from models import OrderItem
    output=[]
    for order in order_ids:
        for sale in session.execute(text('SELECT * FROM bundle_sales WHERE order_id=:id ORDER BY id'),{'id':order}).mappings():
            snapshot=json.loads(sale['snapshot'])
            ids=[r[0] for r in session.execute(text('SELECT order_item_id FROM bundle_fulfillment WHERE sale_id=:id'),{'id':sale['id']})]
            items=[]
            for id in ids:
                row=session.get(OrderItem,id)
                items.append(dict(product_name=row.product.name,quantity=row.quantity,
                    unit=__import__('product_units').historical(session,row),
                    product_id=row.product_id,warehouse_id=row.warehouse_id))
            output.append(dict(id=sale['id'],order_id=order,bundle_id=sale['bundle_id'],
                name=snapshot['name'],category_id=snapshot['category_id'],category_name=snapshot.get('category_name',''),quantity=sale['quantity'],
                amount=sale['amount'],units=snapshot['config']['units'],
                show_flavors=bool(snapshot['config'].get('components') or snapshot['config'].get('pool')) and (snapshot.get('inventory_mode')=='independent' or snapshot['config']['show_flavors']),item_ids=ids,items=items))
    return output


def public_catalog(session, reviewed=True):
    from models import Product
    result=[]
    for row in session.execute(text('SELECT * FROM bundle_definitions WHERE deleted=0')).mappings():
        category=session.get(Product,row['category_id'])
        products={p.id:p for p in session.query(Product).filter_by(parent_id=row['category_id'])}
        cfg=json.loads(row['config'])
        error=None
        import bundle_inventory
        try:validate_config(category,cfg,products,inventory_mode=bundle_inventory.view(session,row['id'])['mode'])
        except ValueError as exc:error=str(exc)
        result.append(dict(id=row['id'],name=row['name'],category_id=row['category_id'],
            category_name=category.name if category else '',config=cfg,
            sellable=bool(row['enabled']) and not error,error=error,
            aliases=[r[0] for r in session.execute(text("SELECT display_name FROM bundle_names WHERE bundle_id=:id AND kind='alias'"),{'id':row['id']})],
            flavors=[dict(id=p.id,name=p.name,parent_name=category.name,parent_id=category.id,
                default_warehouse_id=p.default_warehouse_id) for p in products.values() if p.is_active and not p.is_archived]))
    if reviewed:
        from bundle_aliases import effective
        for bundle in result:
            bundle['aliases'] = effective(bundle)
    return result


def preview(database, session, user, requests, ordinary, explicit_warehouse_id=None,
            cross_warehouse_confirmed=False):
    """Read-only quote. Final recording always revalidates under its write lock."""
    from models import Product,Warehouse
    from warehouse_allocation import select_warehouse,CrossWarehouseRequired
    import config,package_rules
    ordinary,expanded,sales=full_plan(session,requests,user,list(ordinary),explicit_warehouse_id)
    prepared=[]
    for item in ordinary+expanded:
        product=session.get(Product,item['product_id']);warehouse=session.get(Warehouse,item['warehouse_id'])
        unit=item.get('bundle_unit_price')
        if unit is None:unit=database.get_user_unit_price(user.id,product)
        prepared.append(dict(item,product=product,warehouse=warehouse,unit_price=unit))
    ordinary_prepared=[i for i in prepared if i.get('bundle_instance') is None]
    cap=int(config.ORDER_AUTO_SPLIT_UNITS or 6)
    ordinary_packages=[]
    for warehouse in sorted({i['warehouse_id'] for i in ordinary_prepared}):
        ordinary_packages.extend(package_rules.split_dict_items_by_category(
            [i for i in ordinary_prepared if i['warehouse_id']==warehouse],cap,config.CATEGORY_SPLIT_LIMITS))
    limit=session.execute(text('SELECT max_portions FROM bundle_settings WHERE id=1')).scalar()
    grouped=packages(prepared,sales,ordinary_packages,limit)
    from order_result import quote_packages
    result,_=quote_packages(database,grouped,config.SHIPPING_MODE)
    result['bundle_requests']=requests
    result['_ordinary_items']=ordinary
    result['_cross_warehouse_pending']=len({i['warehouse_id'] for i in prepared})>1
    return result
