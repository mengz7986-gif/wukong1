"""One physical parcel plan shared by Bot quotes and transactional shipping."""
import re
from decimal import Decimal

def value(obj,key,default=None):
    return obj.get(key,default) if isinstance(obj,dict) else getattr(obj,key,default)

def pod(row):
    from config import SHIPPING_POD_KEYWORDS
    product=row.get('product')
    parent=value(product,'parent')
    name=value(product,'parent_name') or value(parent,'name') or row.get('parent_name','')
    unit=row.get('unit') or value(product,'unit')
    blob=' '.join(str(v or '') for v in (value(product,'name'),name,row.get('product_name'))).casefold()
    words=[w.strip().casefold() for part in SHIPPING_POD_KEYWORDS for w in re.split(r'[,，、;；]',part) if w.strip()]
    return unit=='颗' or any(w in blob for w in words)

def plan(rows):
    from config import CATEGORY_SPLIT_LIMITS,UNITS_PER_PACKAGE,SHIPPING_POD_QTY
    from package_rules import split_dict_items
    groups={};sizes={}
    for original in rows:
        row=dict(original)
        if int(row.get('quantity') or 0)<=0:continue
        p=row.get('product')
        category=row.get('category_id') or row.get('parent_id') or value(p,'parent_id')
        warehouse=row.get('warehouse_id') or value(p,'default_warehouse_id')
        configured=CATEGORY_SPLIT_LIMITS.get(str(category))
        kind=('category',str(category)) if configured else ('pod' if pod(row) else 'normal')
        key=(warehouse,kind)
        sizes[key]=int(configured or (SHIPPING_POD_QTY if pod(row) else UNITS_PER_PACKAGE) or 6)
        groups.setdefault(key,[]).append(row)
    return [package for key,items in groups.items() for package in split_dict_items(items,sizes[key])]

def fee(package):
    from config import SHIPPING_NORMAL_FEE_AMOUNT,SHIPPING_POD_FEE_AMOUNT
    from warehouse_shipping import read
    rules=read()['rules'];rates=[]
    for row in package:
        p=row.get('product')
        warehouse=row.get('warehouse_id') or value(p,'default_warehouse_id')
        category=row.get('category_id') or row.get('parent_id') or value(p,'parent_id')
        rule=rules.get(f'{warehouse}:{category}')
        rates.append(Decimal(str(rule['fee'] if rule else SHIPPING_POD_FEE_AMOUNT if pod(row) else SHIPPING_NORMAL_FEE_AMOUNT)))
    # One parcel is billed once, never once per flavor or six-unit segment.
    return float(max(rates,default=Decimal('0')))

def total(rows):
    return float(sum((Decimal(str(fee(p))) for p in plan(rows)),Decimal('0')))
