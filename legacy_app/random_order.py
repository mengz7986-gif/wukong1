"""Explicit purchase-random authorization, not out-of-stock fallback."""
import hashlib
import random
import re

def expand(raw_text, products, stocks):
    from category_evidence import prepare
    from rule_dictionary import read_store
    from order_structure import structure_order_text
    text=structure_order_text(prepare(raw_text,products,read_store().get('aliases',[])),products).text
    categories={p.get('parent_name') for p in products if p.get('is_active',True) and not p.get('is_archived')}
    category=None; requests=[]
    for line in text.splitlines():
        line=line.strip().rstrip(':：')
        heading=re.sub(r'^(?:品类|型号)\s*[:：]\s*', '', line)
        if heading in categories:category=heading;continue
        if line in categories:category=line;continue
        match=re.fullmatch(r'(?:不同口味\s*)?(?:随机|随便)\s*(\d+)\s*(?:支|根|个(?:口味)?)(?:\s*[（(]?\s*不同口味(?:谢谢)?\s*[）)]?)?',line)
        if match:
            if not category:raise ValueError('随机订单需要明确品类')
            requests.append((category,int(match.group(1)),line))
    if not requests:return None
    # Mixed fixed/random orders are left to full AI review; never lose fixed lines.
    other=[line for line in text.splitlines() if re.search(r'\d+\s*(?:支|根|个)|[^\d\s]\d+$',line)
           and not re.search(r'随机|随便|共|总|手机|地址|省|市|区|县|路|楼|栋|室|1[3-9]\d{9}',line)]
    if other:return None
    rng=random.Random(hashlib.sha256(raw_text.encode()).digest())
    items=[]
    for category,quantity,evidence in requests:
        pool={}
        for p in products:
            if p.get('parent_name')!=category or not p.get('is_active',True) or p.get('is_archived'):continue
            if int(stocks.get(p['id'],0))<=0:continue
            pool.setdefault(p['name'],p)
        if quantity<=0 or quantity>len(pool):
            raise ValueError(f'{category}可履约的不同口味不足：需要{quantity}个，当前{len(pool)}个')
        chosen=rng.sample(sorted(pool),quantity)
        items.extend((f'品类({category})口味({name})',1) for name in chosen)
    return items
