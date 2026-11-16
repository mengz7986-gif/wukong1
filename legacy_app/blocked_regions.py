"""Existing blacklist scopes; administrative parsing never uses AI."""
from address_utils import normalize_cn_address

MODES={'province','city','detail','contains','exact'}

def validate(address,mode):
    if mode not in MODES: raise ValueError('异常地址类型无效')
    meta=normalize_cn_address(address)
    # The general Contact normalizer may infer a homonymous county from a
    # city-only name (乌鲁木齐市 -> 乌鲁木齐县). A city rule must not acquire
    # that narrower, administrator-unspecified scope.
    if mode=='city' and meta.get('city') and str(address).strip().endswith(meta['city']):
        meta=dict(meta,district='')
    if mode=='province' and not meta.get('province'): raise ValueError('无法可靠识别省份，请填写标准省份名称')
    if mode=='city' and not (meta.get('province') and (meta.get('city') or meta.get('district'))):
        raise ValueError('无法可靠识别市区，请填写省份及市区')
    return meta

def matches(row,address,normalize):
    mode=(row.match_mode or 'contains').lower()
    if mode in {'province','city'}:
        rule=validate(row.address_text,mode); target=normalize_cn_address(address)
        if target.get('province')!=rule.get('province'):return False
        if mode=='province':return True
        for field in ('city','district'):
            if rule.get(field) and target.get(field)!=rule[field]:return False
        return True
    target=row.normalized or normalize(row.address_text); actual=normalize(address)
    if not target or not actual:return False
    if mode=='exact':return actual==target
    return target in actual or (mode=='contains' and len(actual)>=8 and actual in target)

def priority(row):
    return ({'province':1,'city':2}.get(row.match_mode,3),len(row.normalized or ''),row.id or 0)

def rejection(row):
    return f'❌ 下单失败\n当前地址「{row.address_text}」因「{row.reason or "地址异常"}」不接单。'
