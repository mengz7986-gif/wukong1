"""Narrow only a completely literal, explicit-brand merchandise template.

Unknown lines/flavors/brands/aliases/backup instructions retain the full catalog.
The complete customer text and all downstream correctness gates are unchanged.
"""
import re


def scope_catalog(raw_text,catalog):
    if not isinstance(raw_text,str) or len(raw_text)>16000 or not isinstance(catalog,list) or len(catalog)>2000:
        return catalog
    lines=[line.strip() for line in raw_text.splitlines() if line.strip()]
    if len(lines)>128:return catalog
    if any(not isinstance(row,dict) for row in catalog):return catalog
    categories={str(row.get('category') or '') for row in catalog}
    if '' in categories or not categories:return catalog
    pools={category:{str(row.get('flavor') or '') for row in catalog if row.get('category')==category} for category in categories}
    current=None;chosen=set();goods=0;patterns={}
    for line in lines:
        if re.match(r'^(?:收件人|收货人|姓名|联系人|电话|手机号|手机号码|地址|收货地址|所在地区|详细地址)\s*[:：]',line):
            # A contact-labelled mixed line can still contain merchandise.
            # Do not remove its brand from the prompt merely because a label
            # appeared first. False-positive fallback is intentionally safe.
            probe=line.casefold()
            if any(category.casefold() in probe or any(name and name.casefold() in probe for name in pools[category]) for category in categories):
                return catalog
            continue
        if line in ('下单','订单','购买') or re.fullmatch(r'(?:共|合计|总计)\s*\d+\s*(?:支|根|个|件)',line):continue
        if line in categories:
            current=line;chosen.add(line);continue
        if current is None:return catalog
        if line in pools[current]:return catalog
        # A second brand anywhere in an item row is not a literal single-brand
        # template. Keep the complete catalog instead of selecting a subset.
        if any(category!=current and category.casefold() in line.casefold() for category in categories):return catalog
        if current not in patterns:
            names=sorted(pools[current]-{''},key=lambda value:(-len(value),value))
            if not names:return catalog
            patterns[current]=re.compile('(?:'+'|'.join(re.escape(name) for name in names)+r')\s*(?:[xX×*]\s*)?\d+\s*(?:支|根|个|件)?(?=$|[\s,，、;；])')
        position=0;matched=0
        while position<len(line):
            separator=re.match(r'[\s,，、;；]+',line[position:])
            if separator:position+=separator.end();continue
            item=patterns[current].match(line,position)
            if not item:return catalog
            position=item.end();matched+=1
        if not matched:return catalog
        goods+=matched
    if not goods or not chosen or chosen==categories:return catalog
    return [row for row in catalog if row.get('category') in chosen]
