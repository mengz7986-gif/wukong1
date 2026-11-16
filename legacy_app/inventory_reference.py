"""Read-only reference estimates. Never used for FIFO, order costs or profit."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def price_state(cost_price, state=None):
    if state in ('SET', 'UNSET', 'UNKNOWN_LEGACY'):
        return state
    if state is not None:
        raise ValueError('参考成本状态无效')
    try:
        value=Decimal(str(cost_price))
        return 'SET' if value.is_finite() and value>0 else 'UNKNOWN_LEGACY'
    except InvalidOperation:
        return 'UNKNOWN_LEGACY'


def reference_amount(quantity, cost_price, state='SET'):
    qty = Decimal(str(quantity))
    if not qty.is_finite() or qty < 0:
        raise ValueError('库存数量无效')
    if qty == 0:
        return {'reference_value': '0.00', 'reference_incomplete': False}
    if price_state(cost_price,state) != 'SET':
        return {'reference_value': None, 'reference_incomplete': True}
    if cost_price is None or str(cost_price).strip() == '':
        return {'reference_value': None, 'reference_incomplete': True}
    try:
        price = Decimal(str(cost_price))
    except InvalidOperation:
        price = Decimal('NaN')
    if not price.is_finite() or price < 0:
        return {'reference_value': None, 'reference_incomplete': True}
    amount = (qty * price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return {'reference_value': str(amount), 'reference_incomplete': False}


def reference_total(rows):
    """A partial sum is labeled incomplete, never presented as a full value."""
    amounts = [reference_amount(r['quantity'], r.get('cost_price'),r.get('reference_price_state','SET')) for r in rows]
    total = sum((Decimal(a['reference_value']) for a in amounts
                 if a['reference_value'] is not None), Decimal('0.00'))
    return {'reference_value': str(total),
            'reference_incomplete': any(a['reference_incomplete'] for a in amounts)}


def reference_summary(rows):
    """Same item valuation as inventory pages, with explicit price coverage."""
    total=Decimal('0.00')
    result=dict(physical_quantity=0,priced_quantity=0,explicit_zero_quantity=0,
                missing_quantity=0,pending_quantity=0,missing_products=0,pending_products=0)
    for row in rows:
        qty=int(row['quantity'])
        state=price_state(row.get('cost_price'),row.get('reference_price_state'))
        amount=reference_amount(qty,row.get('cost_price'),state)
        result['physical_quantity']+=qty
        if qty==0:continue
        if amount['reference_value'] is not None:
            total+=Decimal(amount['reference_value']);result['priced_quantity']+=qty
            if Decimal(str(row['cost_price']))==0:result['explicit_zero_quantity']+=qty
        elif state=='UNKNOWN_LEGACY':
            result['pending_quantity']+=qty;result['pending_products']+=1
        else:
            result['missing_quantity']+=qty;result['missing_products']+=1
    result.update(reference_value=str(total),reference_incomplete=bool(result['missing_quantity'] or result['pending_quantity']))
    assert result['physical_quantity']==result['priced_quantity']+result['missing_quantity']+result['pending_quantity']
    return result
