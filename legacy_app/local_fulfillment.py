"""Explicit customer authorization, resolved only within the real category."""
import re
from fulfillment import POLICY_NONE, POLICY_ALTERNATE, POLICY_RANDOM, POLICY_REPEAT_SELECTED, POLICY_EXACT
from deepseek_resolver import resolve_identity_flavor as resolve_flavor
from order_structure import structure_order_text, fulfillment_parts, catalog_names, syntax_text

def attach(info, products, resolve_item, stock_of=None, defer_backup=False, is_available=None):
    names=catalog_names(products)
    notes=structure_order_text(info.get('raw_text') or '',products=products).fulfillment_notes
    if not notes:return info
    canonical=[]
    for name,qty in info.get('items') or []:
        product,error=resolve_item(name)
        if product is None:return info
        canonical.append(dict(category=product.get('parent_name'),flavor=product['name'],quantity=qty))
    if not canonical:return info
    # Backup interpretation is a shortage-only operation. The early parser
    # path has no stock snapshot; defer it to the ordinary preview validator.
    if defer_backup:return info
    short_keys=None
    if stock_of is not None:
        from collections import Counter
        wanted=Counter()
        for item in canonical:wanted[(item['category'],item['flavor'])]+=item['quantity']
        def available(product,needed):
            return (is_available(product,needed) if is_available is not None
                    else int(stock_of(product['id']) or 0)>=needed)
        short_keys={key for key,needed in wanted.items() if not any(
            (p.get('parent_name'),p.get('name'))==key and p.get('is_active',True)
            and not p.get('is_archived') and available(p,needed) for p in products)}
        if not short_keys:
            return dict(info,ai_fulfillment={'on_out_of_stock':POLICY_NONE},ai_alternates=[])
    from ai_flavor_decision import selected_mutual_pairs
    pairs=selected_mutual_pairs(info.get('raw_text') or '',canonical,products)
    if pairs:
        return dict(info,ai_items=canonical,
                    ai_fulfillment={'on_out_of_stock':POLICY_ALTERNATE,'selection_scope':'selected_mutual'},
                    ai_alternates=[dict(**{'for':a},flavor=b) for a,b in pairs])
    note_categories=[category for _,category in scoped_notes(info.get('raw_text') or '',canonical,products)]
    alternates=[];policy=POLICY_NONE
    for note_index,note in enumerate(notes):
        if re.fullmatch(r'\s*(?:替补|备选|备用)\s*[:：]?\s*已选的?(?:味道|口味)(?:里面|里)?补\s*',note):
            policy=POLICY_REPEAT_SELECTED;continue
        if re.search(r'(?:^|[\s，,])精准发货\s*$', syntax_text(note,names)):
            policy=POLICY_EXACT;continue
        if re.search(r'(?:缺货|没货)(?:就)?\s*(?:发|用)?\s*重复',syntax_text(note,names)):
            policy=POLICY_REPEAT_SELECTED;continue
        if re.search(r'(?:缺货|没货|备选|备用)\s*[:：]?\s*随机',syntax_text(note,names)):
            policy=POLICY_RANDOM;continue
        parts=fulfillment_parts(note,names)
        if not parts:continue
        prefix,body,preferred=parts
        if body=='精准发货' and not prefix:
            policy=POLICY_EXACT;continue
        if body=='互为备选':
            from ai_flavor_decision import mutual_backup_pairs
            authorized=mutual_backup_pairs(note,canonical,products)
            if not authorized and not prefix.strip(' ()（）,，;；'):
                authorized=mutual_backup_pairs(info.get('raw_text') or '',canonical,products)
            if not authorized:raise ValueError('互为备选未能关联到原文同一品类口味，请明确首选与备选')
            policy=POLICY_ALTERNATE
            alternates.extend(dict(**{'for':a},flavor=b,source_text=note) for a,b in authorized)
            continue
        if body.strip() in ('无','没有','无备选','暂无'):continue
        prefix=prefix.strip(' ()（）,，;；')
        if preferred:prefix=preferred
        selected=[item for item in canonical if item['flavor'] in prefix]
        if not prefix.strip() and len(canonical)==1:selected=canonical
        if not prefix.strip() and len({item['category'] for item in canonical})==1:
            selected=canonical
        if not prefix.strip() and not selected and note_index<len(note_categories) and note_categories[note_index]:
            selected=[item for item in canonical if item['category']==note_categories[note_index]]
        if not selected or len({item['category'] for item in selected})!=1:
            raise ValueError('备选未能关联到唯一品类，请明确首选与备选')
        # A shortage elsewhere in the order does not activate this flavor's
        # backup. Do not even resolve or validate an unused backup spelling.
        if short_keys is not None:
            selected=[main for main in selected if (main['category'],main['flavor']) in short_keys]
            if not selected:continue
        item=selected[0]
        pool=[p for p in products if p.get('parent_name')==item['category'] and p.get('is_active',True)]
        flavor_names=list(dict.fromkeys(p['name'] for p in pool))
        options=[]
        for raw in re.split(r'[,，、/／]|或者|或',body):
            if not raw.strip():continue
            flavor,_=resolve_flavor(item['category'],raw.strip(),flavor_names,products=pool)
            if not flavor:raise ValueError('备选口味无法唯一识别，请明确目录口味或使用已启用叫法')
            if flavor not in options:options.append(flavor)
        if not options:raise ValueError('缺货备选未填写有效口味')
        policy=POLICY_ALTERNATE
        alternates.extend(dict(**{'for':main['flavor']},flavor=f,source_text=note) for main in selected for f in options)
    if policy==POLICY_NONE:return info
    if policy==POLICY_ALTERNATE:
        for alternative in alternates:
            matching_categories={row['category'] for row in canonical if row['flavor']==alternative['for']}
            if len(matching_categories)>1:
                raise ValueError('备选需要确认：多个品类包含同名首选口味，无法安全关联备选；请按品类分开确认，未自动替换。')
    return dict(info,ai_items=canonical,ai_fulfillment={'on_out_of_stock':policy},ai_alternates=alternates)


def scoped_notes(raw_text, items, products):
    """Bind existing fulfillment spans to explicit source categories, no guess."""
    from category_evidence import prepare
    from rule_dictionary import runtime_store
    from contact_fields import split_inline_catalog_goods
    contextual=prepare(split_inline_catalog_goods(raw_text,products),products,runtime_store().get('aliases',[])).splitlines()
    categories={item['category'] for item in items}
    current=None;output=[];names=catalog_names(products)
    for line in contextual:
        if re.fullmatch(r'\s*(?:共计|总共|总计|合计|一共|共|订单总数)\s*[:：]?\s*\d+\s*(?:支|只|根|个|件|条)?\s*',line):
            current=None;continue
        heading=re.sub(r'^(?:品类|型号)[:：]\s*','',line.strip())
        matches=[category for category in categories if heading==category or re.match(re.escape(category)+r'(?=[\s:：0-9])',heading)]
        if len(matches)==1:current=matches[0]
        parts=fulfillment_parts(line,names)
        if parts and parts[1].strip() not in ('无','没有','无备选','暂无'):output.append((line,current))
    return output
