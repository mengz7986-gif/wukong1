"""Customer guidance only; original technical diagnostics remain unchanged."""
import re

def public_order_reasons(reasons,*,unresolved=(),raw_text=''):
    output=[]
    for row in unresolved[:8]:
        if not isinstance(row,dict):continue
        flavor=str(row.get('raw_flavor') or '').strip();category=str(row.get('raw_category') or '').strip()
        flavor=flavor if flavor and flavor in raw_text and len(flavor)<=64 else ''
        category=category if category and category in raw_text and len(category)<=64 else ''
        label=flavor or category
        if label:
            label=re.sub(r'[\r\n\x00-\x1f]',' ',label)
            value=('请确认「'+label+'」属于哪个商品分类' if not row.get('category') else '未找到「'+label+'」口味')
            if value not in output:output.append(value)
    for reason in reasons[:8]:
        value=str(reason)
        if re.search(r'TIMEOUT|ReadTimeout|超时',value,re.I):value='订单识别暂时失败，请稍后重新提交。'
        elif re.search(r'AI\s*未能确定字段|AI\s*未识别到可安全匹配|AI\s*商品.*(?:匹配|目录)',value):
            if output:continue
            value='请补充具体商品分类及口味。'
        elif '置信度' in value or re.search(r'confidence|Validator|source_text|INVALID_RESPONSE|DeepSeek|AI\s*标记|Traceback|safety|\bflavor\b|\bcategory\b|\bquantity\b',value,re.I):
            value='商品信息暂无法可靠确认，请核对商品分类、口味及数量。'
        if value and value not in output:output.append(value)
    return output[:8] or ['订单信息无法完整识别，请核对收件信息、商品及数量。']
