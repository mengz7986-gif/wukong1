"""Field-scoped spelling equivalence only, never geographic proof or renaming."""
import re,unicodedata
def variants(field,value):
    value=re.sub(r'\s+','',unicodedata.normalize('NFKC',str(value or '')))
    result={value} if value else set()
    if field=='province' and value.endswith('省') and len(value)>2:result.add(value[:-1])
    if field=='city' and value.endswith('市') and len(value)>2:result.add(value[:-1])
    if field=='province' and value in {'北京','北京市','天津','天津市','上海','上海市','重庆','重庆市'}:
        result={value.removesuffix('市'),value.removesuffix('市')+'市'}
    return result
def same(field,left,right):return bool(variants(field,left)&variants(field,right))
def represented_at_source_start(raw,field,value,fields):
    raw=re.sub(r'\s+','',raw)
    if field=='province':return any(raw.startswith(v) for v in variants(field,value))
    if field=='city':
        starts={raw}
        for p in variants('province',fields.get('province')):
            if raw.startswith(p):starts.add(raw[len(p):])
        return any(part.startswith(v) for part in starts for v in variants(field,value))
    return False
