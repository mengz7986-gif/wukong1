"""Exact-ID cleanup for proven inactive, unreferenced dictionary rows."""
import hashlib
import json
from datetime import datetime, timezone
import brand_aliases
import rule_dictionary as dictionary


def active_signature(store):
    values=[]
    for row in store.get('aliases',[]):
        if row.get('status')=='active':
            values.append(tuple(row.get(k) for k in ('id','type','alias','standard','category','scope','warehouse_id','category_id','product_id','bundle_id')))
    return hashlib.sha256(json.dumps(sorted(values,key=str),ensure_ascii=False).encode()).hexdigest()


def remove_disabled(ids, actor, expected_file_sha256):
    ids=set(ids)
    if not ids or any(not isinstance(value,str) or not value.startswith('manual-') for value in ids):
        raise ValueError('清理ID清单无效')
    current=hashlib.sha256(dictionary.path().read_bytes()).hexdigest()
    if current!=expected_file_sha256:raise ValueError('词典已变化，必须重新审计')
    def change(store):
        before=active_signature(store)
        rows=[r for r in store.get('aliases',[]) if r.get('id') in ids]
        if {r.get('id') for r in rows}!=ids:raise ValueError('清理对象已变化')
        if any(r.get('readonly') or r.get('status')!='disabled' for r in rows):
            raise ValueError('仅允许清理已停用的人工叫法')
        referenced={value for candidate in store.get('brand_candidates',[])
                    for value in candidate.get('existing_alias_ids') or []}
        if ids & referenced:raise ValueError('叫法仍被审核记录引用')
        store['aliases']=[r for r in store['aliases'] if r.get('id') not in ids]
        now=datetime.now(timezone.utc).isoformat()
        for row in rows:
            store.setdefault('audit',[]).append(dict(actor=actor,action='safe_delete_disabled_alias',id=row['id'],at=now))
        store['updated_at']=now
        after=active_signature(store)
        if after!=before:raise ValueError('有效识别集发生变化，已拒绝清理')
        return {'removed_ids':sorted(ids),'active_signature':after,'removed_count':len(rows)}
    return brand_aliases.transaction(change)
