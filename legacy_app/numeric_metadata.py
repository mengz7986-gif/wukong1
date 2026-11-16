"""Source-labelled references/bills/address details, never merchandise counts."""
import re

DETAIL_LABEL=r'(?:门牌号|门牌|房间号|房号|室号|楼栋|楼层|单元号)'
DETAIL_VALUE=r'[A-Za-z0-9０-９一二三四五六七八九十号楼栋幢单元室层\-－]{1,40}'

def separate(value,products=(),names=None):
    from order_structure import syntax_text,catalog_names,_labelled_amount,_amount_with_note
    from parser_rules import normalize_order_text
    protected=catalog_names(products) if names is None else names
    refs=[];amounts=[];lines=[]
    reference=re.compile(r'(?<![\w\u4e00-\u9fff])(?:客户订单号|客户单号|订单编号|订单号|订单|单号|编号|序号)\s*[:：#]?\s*'
        r'([A-Za-z0-9０-９][A-Za-z0-9０-９_\-]{0,39})(?![A-Za-z0-9０-９_\-支根个件号栋室楼层幢])')
    money_labels=r'(?:支付金额|实收金额|实收|总金额|订单金额|商品金额|付款金额|合计金额|应付总额|实付金额|金额合计|合计货款|总价|金额|总额|付款|收款|货款|应付|实付|运费|优惠|余额|欠款)'
    number=r'[¥￥]?\s*\d+(?:\.\d{1,2})?'
    expression=number+r'(?:\s*[+\-*/×÷xX]\s*'+number+r')*(?:\s*[=＝]\s*'+number+r')?'
    money=re.compile(r'(?<![\w\u4e00-\u9fff])(?:'+money_labels+r'\s*[:：=]?\s*'+expression+
        r'\s*(?:元|RMB|CNY)?|(?:合计|总计)\s*[:：]?\s*(?:[¥￥]\s*\d+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s*元))(?![\d支根个件])',re.I)
    bill=re.compile(r'(?<!\d)(?:[¥￥]\s*\d+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s*元|'
        +number+r'(?:\s*[+\-*/×÷xX]\s*'+number+r')+\s*[=＝]\s*'+number+r')(?![\d支根个件])',re.I)
    detail=re.compile(r'(?<![\w\u4e00-\u9fff])'+DETAIL_LABEL+r'\s*[:：]\s*'+DETAIL_VALUE)
    for line in str(value or '').splitlines():
        if re.fullmatch(r'\s*[¥￥]?\d+(?:\.\d{1,2})?\s+[A-Za-z]+\d{1,3}(?:仓)?\s*',line):
            # Existing context-aware dispatch/bill splitter owns this pair.
            lines.append(line);continue
        if _labelled_amount(line,protected) or _amount_with_note(line,protected):
            amounts.append(normalize_order_text(line.strip()));lines.append('');continue
        probe=syntax_text(line,protected);spans=[]
        for m in reference.finditer(probe):spans.append((m.start(),m.end(),'ref',m.group(1)))
        for m in re.finditer(r'(?<!\w)#\s*(\d{1,40})(?![\d支根个件])',probe):
            spans.append((m.start(),m.end(),'ref',m.group(1)))
        for m in money.finditer(probe):spans.append((m.start(),m.end(),'money',line[m.start():m.end()].strip()))
        if not re.search(r'地址|省|市|区|县|路|街|楼|栋|室',probe):
            for m in bill.finditer(probe):spans.append((m.start(),m.end(),'money',line[m.start():m.end()].strip()))
        for m in detail.finditer(probe):spans.append((m.start(),m.end(),'detail',None))
        occupied=set()
        for start,end,kind,note in sorted(spans,key=lambda span:span[1]-span[0],reverse=True):
            if any(i in occupied for i in range(start,end)):continue
            occupied.update(range(start,end))
            if kind=='ref':refs.append(note)
            elif kind=='money':amounts.append(note)
        chars=list(line)
        for index in occupied:chars[index]=' '
        cleaned=''.join(chars)
        cleaned=re.sub(r'[（(\[【]\s*[）)\]】]',' ',cleaned)
        # Correct only a complete count annotation; preserve it as a total
        # so a mismatch still blocks submission instead of silently dropping it.
        for m in reversed(list(re.finditer(r'[（(]\s*工\s*([一1])\s*支\s*[）)]',syntax_text(cleaned,protected)))):
            cleaned=cleaned[:m.start()]+'\n共1支\n'+cleaned[m.end():]
        lines.append(cleaned)
    return '\n'.join(lines),tuple(refs),tuple(amounts)
