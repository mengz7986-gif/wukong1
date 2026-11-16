"""Read-only sale warnings. Reference purchase prices are never FIFO evidence."""
import hashlib,json,math,re
from decimal import Decimal,InvalidOperation
from sqlalchemy import text

def amount(value):
    if isinstance(value,bool) or not isinstance(value,(int,float,str,Decimal)):
        raise ValueError('金额格式无效')
    raw=str(value).strip()
    if not re.fullmatch(r'\d+(?:\.\d{1,2})?',raw):raise ValueError('金额须为非负数且最多两位小数')
    try:number=Decimal(raw)
    except InvalidOperation:raise ValueError('金额格式无效') from None
    if not number.is_finite() or number>Decimal('1000000'):raise ValueError('金额超出允许范围')
    return float(number)

def cost_evidence(session,product):
    pid=product.id;wid=product.default_warehouse_id
    stock=session.execute(text('SELECT quantity FROM warehouse_stock WHERE product_id=:p AND warehouse_id=:w'),dict(p=pid,w=wid)).scalar()
    batches=[dict(r) for r in session.execute(text('''SELECT id,remain_qty,unit_cost,cost_source,fifo_priority
        FROM inventory_batches WHERE product_id=:p AND warehouse_id=:w AND remain_qty>0 ORDER BY fifo_priority,id'''),dict(p=pid,w=wid)).mappings()]
    unknown=[dict(r) for r in session.execute(text('''SELECT id,remain_qty,status FROM inventory_unknown_cost
        WHERE product_id=:p AND warehouse_id=:w AND remain_qty>0'''),dict(p=pid,w=wid)).mappings()]
    known=bool(stock and stock>0 and batches and not unknown and sum(r['remain_qty'] for r in batches)==stock
        and all(r['cost_source'] in ('VERIFIED_REAL_COST','RESTORED_VERIFIED_FIFO') and r['unit_cost'] is not None
                and math.isfinite(r['unit_cost']) and r['unit_cost']>=0 for r in batches))
    evidence=dict(product_id=pid,warehouse_id=wid,stock=stock,batches=batches,unknown=unknown)
    return dict(known=known,cost_min=min(r['unit_cost'] for r in batches) if known else None,
        cost_max=max(r['unit_cost'] for r in batches) if known else None,
        fingerprint=hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest())

def review(session,product,sale):
    sale=amount(sale);cost=cost_evidence(session,product);warnings=[]
    if sale==0:warnings.append('当前售价为0元，是否确认保存？')
    if not cost['known']:warnings.append('当前商品成本尚未确认，无法准确计算预计利润。')
    elif sale<cost['cost_max']:warnings.append('⚠️ 当前售价低于成本价，预计产生亏损，是否确认保存？')
    return dict(sale=sale,cost=cost,warnings=warnings,
        estimated_profit_min=sale-cost['cost_max'] if cost['known'] else None,
        estimated_profit_max=sale-cost['cost_min'] if cost['known'] else None,
        boundary='当前仓库剩余可信FIFO成本区间，非未来订单成交利润；实际订单按成交时FIFO记录')

def require_confirmation(session,pairs,context,token):
    """Initial call is read-only; confirmation binds target, price and FIFO facts."""
    from fastapi import HTTPException
    import secrets
    try:rows=[dict(product_id=p.id,review=review(session,p,sale)) for p,sale in sorted(pairs,key=lambda x:x[0].id)]
    except ValueError as error:raise HTTPException(400,str(error)) from None
    messages=list(dict.fromkeys(m for r in rows for m in r['review']['warnings']))
    if context.get('requested_price')==0 and not any('0元' in m for m in messages):messages.append('当前售价为0元，是否确认保存？')
    if not messages:return
    fingerprint=hashlib.sha256(json.dumps(dict(context=context,rows=rows),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if not isinstance(token,str) or not secrets.compare_digest(token,fingerprint):
        raise HTTPException(409,dict(code='PRICE_CONFIRMATION_REQUIRED',token=fingerprint,messages=messages,
            product_count=len(rows),message='请核对价格风险并明确确认；取消不会保存。'))

def catalog_confirmation(session,products,updates,token,context):
    """All catalog selling tiers use the same FIFO evidence and confirmation."""
    from fastapi import HTTPException
    fields=('level1_agent_price','level2_agent_price','regular_price')
    try:prices={k:amount(v) for k,v in updates.items() if k in fields and v is not None}
    except ValueError as error:raise HTTPException(400,str(error)) from None
    if not prices:return
    before=[dict(id=p.id,warehouse=p.default_warehouse_id,prices={k:getattr(p,k,None) for k in fields}) for p in products]
    require_confirmation(session,[(p,price) for p in products for price in prices.values()],
        dict(context=context,updates=prices,before=before),token)
