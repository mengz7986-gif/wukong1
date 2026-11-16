"""Offline delivery-address decisions. No providers, guessing or order writes."""
import re
from address_utils import normalize_cn_address
from region_index import get_region_index

LABELS={
    'MISSING_PROVINCE':'省份', 'MISSING_CITY':'城市', 'MISSING_DISTRICT':'区县',
    'MISSING_DETAIL':'详细收货位置（路名/小区/单位及必要门牌）',
    'AMBIGUOUS_REGION':'省市区有重名，需明确所属城市/区县',
    'REGION_CONFLICT':'省市区上级关系冲突',
    'UNVERIFIED_REGION':'行政区名称尚未在离线库核实',
    'DETAIL_LOSS':'楼栋、单元或房号与原文不一致',
    'GOODS_IN_ADDRESS':'地址中混有商品字段',
    'DATA_UNAVAILABLE':'离线行政区数据不可用',
}

def assess(raw):
    value=str(raw or '').strip()
    meta=normalize_cn_address(value)
    index=get_region_index()
    issues=[]
    for key,code in [('province','MISSING_PROVINCE'),('city','MISSING_CITY'),('district','MISSING_DISTRICT')]:
        if not meta.get(key):issues.append(code)
    detail=str(meta.get('detail') or '').strip()
    if (not detail or detail==meta.get('town') or
            re.fullmatch(r'[A-Za-z\d\s号栋幢楼座单元室层房\-—()（）]+',detail)):
        issues.append('MISSING_DETAIL')
    if any(str(w).startswith('行政区重名') for w in meta.get('warnings',[])):
        issues.append('AMBIGUOUS_REGION')
    if any(meta.get(k) for k in ('legacy_region_conflict','unverified_city_scope','named_parent_conflict')):
        issues.append('REGION_CONFLICT')
    p,c,d=(meta.get(k) or '' for k in ('province','city','district'))
    functional_zone=bool(index and p and c and d and any(index._same(p,row['province'])
        and index._same(c,row['city']) and d in row['aliases'] and d in value
        for row in index.delivery_zones))
    if index and p and c and d:
        parents=index.district_parents(d,p,c)
        named=any(row['district']==d and index._same(p,row['province']) and index._same(c,row['city'])
                  for row in index.verified_named_regions)
        direct=(index.is_direct_county_city_view(p,c) and d==c) or (
            index.is_direct_admin_city(c) and (d==c or any(index._same(p,chain.province)
                and index._same(c,chain.city) for chain in index.street_by_name.get(d,()))))
        custom=index.protected_custom_index
        compatible=bool(custom and any(index._same(p,n) for n in custom.province_by_name)
                        and custom.district_parents(d,p,c))
        if not (len(parents)==1 or named or direct or compatible or functional_zone):issues.append('UNVERIFIED_REGION')
    if not index:issues.append('DATA_UNAVAILABLE')
    if re.search(r'(?:下单产品|下单商品|商品|产品|货品|物品|购买)\s*[:：;；]',value):
        issues.append('GOODS_IN_ADDRESS')
    # Delivery identifiers must survive normalization, including hyphenated
    # rooms and ASCII building wings. Do not equate 01 with 1 or 3302 with 302.
    cursor=0;normalized=str(meta.get('normalized_address') or '').casefold()
    delivery_value=re.sub(r'(?:邮政编码|邮编)\s*[:：]?\s*\d{6}(?!\d)','',value)
    for token in re.findall(r'[A-Za-z]*\d+(?:[-—]\d+)*[A-Za-z]*',delivery_value):
        at=normalized.find(token.casefold(),cursor)
        if at<0:issues.append('DETAIL_LOSS');break
        cursor=at+len(token)
    issues=list(dict.fromkeys(issues))
    return dict(valid=not issues,issues=issues,meta=meta,
                delivery_zone_verified=functional_zone,official_county_verified=bool(index and p and c and d and len(index.district_parents(d,p,c))==1),
                source=value,normalized=meta.get('normalized_address') or value,
                snapshot_id=getattr(index,'snapshot_id',''),
                postal_codes=re.findall(r'(?:邮政编码|邮编)\s*[:：]?\s*(\d{6})(?!\d)',value),
                additions=[k for k in ('province','city','district','town')
                           if meta.get(k) and meta[k] not in value])

def problem_text(diagnostics):
    codes=diagnostics.get('address_issues') or ['MISSING_PROVINCE','MISSING_CITY','MISSING_DISTRICT','MISSING_DETAIL']
    labels=[LABELS[c] for c in codes if c in LABELS]
    return '收货地址需要核对：'+'；'.join(labels)+'。请补全或核对后重新发送完整订单；未自动猜测地址。'
