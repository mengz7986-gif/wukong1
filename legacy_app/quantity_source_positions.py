"""Original-source provenance for quantity facts; no business/catalog writes.

Positions are Python character offsets in the ORIGINAL Telegram text, not in
generated canonical lines. Unproven locations are explicit, never fabricated.
"""
import re
from bisect import bisect_left
from dataclasses import asdict
from source_evidence import indexed

def locate(original,facts,products=(),rows=()):
    source,positions=indexed(original)
    starts=[p[0] for p in positions]
    used=set();result=[]
    def span(start,end,kind):
        a,b=positions[start][0],positions[end-1][1]
        return dict(source_start=a,source_end=b,source_text=original[a:b],
                    source_line=original.count('\n',0,a)+1,source_kind=kind)
    for fact in facts:
        item=asdict(fact);found=None
        if fact.kind=='ITEM_QUANTITY':
            label=str(fact.item or '')
            ref=re.fullmatch(r'品类\((.*?)\)口味\((.*)\)',label)
            if ref:label=ref[2]
            labels={label}
            from brand_aliases import scope_matches
            for row in rows:
                if (row.get('type')=='EXPLICIT_FLAVOR' and row.get('status')=='active'
                        and row.get('standard')==label and row.get('category')==fact.section
                        and any(scope_matches(row,p) for p in products if p.get('name')==label and p.get('parent_name')==fact.section)):
                    labels.add(str(row.get('alias') or ''))
            for label in sorted(labels,key=len,reverse=True):
                needle,_=indexed(label)
                if not needle:continue
                for m in re.finditer(re.escape(needle),source):
                    a=positions[m.start()][0];lo=original.rfind('\n',0,a)+1
                    hi=original.find('\n',positions[m.end()-1][1])
                    hi=len(original) if hi<0 else hi
                    left,right=bisect_left(starts,lo),bisect_left(starts,hi)
                    # indexed() removes whitespace, including newlines. Do
                    # not concatenate the next line's price/phone/quantity
                    # onto this item's count when locating its provenance.
                    suffix=re.match(r'[*×x]?\(?(\d+|[一二两三四五六七八九十]+)(?:支|只|根|个|件|条|颗|盒)?\)?',source[m.end():right])
                    prefix=re.search(r'(\d+)(?:支|只|根|个|件|条|颗|盒)$',source[left:m.start()])
                    start,end=m.span();kind='DEFAULT_ONE_EXPLICIT_FLAVOR'
                    from learning_knowledge import _chinese_quantity
                    suffix_qty=(int(suffix[1]) if suffix and suffix[1].isdigit() else
                                _chinese_quantity(suffix[1].replace('两','二')) if suffix else None)
                    if suffix and suffix_qty==fact.quantity:
                        end+=suffix.end();kind='ITEM_SUFFIX'
                    elif prefix and int(prefix[1])==fact.quantity:
                        start=left+prefix.start();kind='ITEM_PREFIX'
                    elif fact.quantity!=1:continue
                    if (start,end) in used:continue
                    # Delivery/contact-only lines cannot prove a flavor count.
                    a=positions[m.start()][0];lo=original.rfind('\n',0,a)+1;hi=original.find('\n',a)
                    line=original[lo:hi if hi>=0 else len(original)]
                    if re.search(r'1[3-9]\d{9}|地址|收件人|电话|详细地址',line):continue
                    found=span(start,end,kind);used.add((start,end));break
                if found:break
        else:
            pattern=r'(?:整单合计|整单|订单合计|订单总数|总数量|一共|总共|总计|总数|数量|支数|共共|共计|合计|小计|共|总)[:：]?(\d+)(?:支|只|根|个|件|条|颗|盒)?'
            for m in re.finditer(pattern,source):
                if int(m[1])==fact.quantity and m.span() not in used:
                    found=span(*m.span(),'EXPLICIT_DECLARATION');used.add(m.span());break
            if not found and fact.kind=='SECTION_SUBTOTAL':
                # Source-proven category xN: declarations are not generated
                # "共N" text. Bind the count to its exact reviewed heading.
                labels={str(fact.section or '')}
                from brand_aliases import scope_matches
                labels.update(str(r.get('alias') or '') for r in rows
                    if r.get('type')=='MODEL_ALIASES' and r.get('status')=='active'
                    and r.get('standard')==fact.section and any(scope_matches(r,p)
                        for p in products if p.get('parent_name')==fact.section))
                for label in sorted(labels,key=len,reverse=True):
                    needle,_=indexed(label)
                    if not needle:continue
                    for m in re.finditer(re.escape(needle)+r'[*×x](\d+)[:：]',source):
                        a=positions[m.start()][0];lo=original.rfind('\n',0,a)+1
                        prefix=original[lo:a].strip()
                        if prefix not in ('','品类：','品类:','型号：','型号:'):continue
                        if int(m[1])==fact.quantity and m.span() not in used:
                            found=span(*m.span(),'CATEGORY_MULTIPLIER_SUBTOTAL');used.add(m.span());break
                    if found:break
                if not found:
                    for label in sorted(labels,key=len,reverse=True):
                        needle,_=indexed(label)
                        if not needle:continue
                        for m in re.finditer(re.escape(needle)+r'(\d+)[:：(（]',source):
                            a=positions[m.start()][0];lo=original.rfind('\n',0,a)+1
                            if original[lo:a].strip() not in ('','品类：','品类:','型号：','型号:'):continue
                            if int(m[1])==fact.quantity and m.span() not in used:
                                found=span(*m.span(),'CATEGORY_DELIMITED_SUBTOTAL');used.add(m.span());break
                        if found:break
            if not found and fact.kind=='SECTION_SUBTOTAL':
                for m in re.finditer(r'(\d+)(?:支|只|根|个|件)(?:口味|味道)[:：]',source):
                    if int(m[1])==fact.quantity and m.span() not in used:
                        found=span(*m.span(),'HEADING_SUBTOTAL');used.add(m.span());break
        item.update(found or dict(source_start=None,source_end=None,source_text=None,
                                  source_line=None,source_kind='UNRESOLVED_PROVENANCE'))
        result.append(item)
    return result
