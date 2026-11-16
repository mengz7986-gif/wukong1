"""Dropship catalog adapter; shared rules and their synchronization hash stay unchanged."""
import re
from dataclasses import replace
from parser_rules import analyze_quantity_scopes,validate_quantity_facts

def analyze(source,parsed_items=None,*,products=(),**kwargs):
    valid={(str(p.get('parent_name') or ''),str(p.get('name') or ''))
           for p in products if p.get('is_active',True) and not p.get('is_archived')}
    restoration={}
    def shield(match):
        category,flavor=match.group(1),match.group(2)
        if (category,flavor) not in valid or not re.match(r'^\d',flavor):return match.group()
        # Opaque, collision-free, invocation-local token; never persisted as a
        # product identity. Restore exact source text in every resulting fact.
        index=len(restoration);token='CATALOGIDENTITY'+('X'* (index+1))
        while token in source:token+='X'
        restoration[token]=flavor
        return '品类('+category+')口味('+token+')'
    masked=re.sub(r'品类\(([^()]+)\)口味\(([^()]+)\)',shield,source)
    # Whole-line explicit summaries are global regardless of the last section.
    from learning_knowledge import _chinese_quantity
    def global_total(match):
        raw=match[1];number=int(raw) if raw.isdigit() else _chinese_quantity(raw.replace('两','二'))
        return '订单总数'+str(number)+'支' if number is not None else match[0]
    masked=re.sub(r'(?m)^\s*(?:共计|总计|合计|总共|一共|共|整单(?:合计|总计)?|订单合计|订单总数|总数量)\s*[:：]?\s*([0-9零一二两三四五六七八九十]+)\s*(?:只|支|根|颗|个|件|条|盒)?\s*$',global_total,masked)
    result=analyze_quantity_scopes(masked,parsed_items,**kwargs)
    def restore(value):
        if value is None:return None
        for token,original in sorted(restoration.items(),key=lambda p:-len(p[0])):value=value.replace(token,original)
        return value
    facts=[]
    for f in result.facts:
        f=replace(f,item=restore(f.item),text=restore(f.text),section=restore(f.section))
        if f.kind=='ORDER_TOTAL' and re.match(r'^小计',f.text):
            prior=[p for p in facts if p.kind=='ITEM_QUANTITY' and p.section]
            if prior:f=replace(f,kind='SECTION_SUBTOTAL',section=prior[-1].section,section_index=prior[-1].section_index)
        facts.append(f)
    return validate_quantity_facts(facts,parsed_items)
