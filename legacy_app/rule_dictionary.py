"""彩虹仓数据型词典：核心只读，人工口味别名独立存储。"""
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar

ROOT = Path(__file__).resolve().parent
_RUNTIME_STORE = ContextVar('rainbow_runtime_dictionary', default=None)


@contextmanager
def runtime_snapshot():
    """One lazy read-only snapshot per parse; mutations always use read_store.

    No cross-message cache: a newly approved/disabled alias is visible to the
    next parse. ContextVar keeps parallel customer requests independent.
    """
    if _RUNTIME_STORE.get() is not None:
        yield
        return
    token = _RUNTIME_STORE.set({})
    try:
        yield
    finally:
        _RUNTIME_STORE.reset(token)


def runtime_store():
    holder = _RUNTIME_STORE.get()
    if holder is None:
        return read_store()
    if 'store' not in holder:
        holder['store'] = read_store()
    return holder['store']


def path():
    return Path(os.getenv('RAINBOW_DICTIONARY_PATH', str(ROOT / 'data' / 'rule_dictionary.json')))


def read_store():
    if not path().exists():
        return {'aliases': [], 'audit': []}
    value = json.loads(path().read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not isinstance(value.get('aliases'), list):
        raise ValueError('词典文件损坏，请恢复备份；不会静默覆盖')
    # Category/product IDs are the association. Display names are current
    # catalog facts, not an independent standard-brand table or a new alias.
    db_url=os.getenv('DATABASE_URL','sqlite:///./orders.db')
    if db_url.startswith('sqlite:///') and (path()==ROOT/'data'/'rule_dictionary.json' or os.getenv('DATABASE_URL')):
        database=Path(db_url[len('sqlite:///'):]).resolve()
        if database.is_file():
            with sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True) as connection:
                connection.row_factory=sqlite3.Row
                catalog=[dict(row) for row in connection.execute(
                    'SELECT id,name,parent_id,is_parent,is_archived FROM products')]
            value=resolve_catalog_targets(value,catalog)
    return value


def resolve_catalog_targets(store,catalog):
    """Resolve names by proven IDs only; unknown/missing relationships stay untouched."""
    import copy
    # This read owns its freshly decoded store. Only these branches are
    # rewritten; copying audit/observation history has no parsing purpose.
    result=dict(store)
    for key in ('aliases','brand_candidates'):
        if key in store:result[key]=copy.deepcopy(store[key])
    by_id={p['id']:p for p in catalog}
    for key in ('aliases','brand_candidates'):
        for row in result.get(key,[]):
            if row.get('readonly') or row.get('status')=='quarantined':continue
            category=by_id.get(row.get('category_id'))
            if not category or not category.get('is_parent') or category.get('is_archived'):continue
            if row.get('type','MODEL_ALIASES')=='MODEL_ALIASES':
                row.update(standard=category['name'],category=category['name'],model=category['name'])
            elif row.get('type')=='EXPLICIT_FLAVOR':
                product=by_id.get(row.get('product_id'))
                if product and not product.get('is_parent') and not product.get('is_archived') and product.get('parent_id')==category['id']:
                    row.update(standard=product['name'],category=category['name'])
    return result


def builtins():
    from deepseek_prompt import MODEL_ALIASES
    from deepseek_resolver import (FLAVOR_SYNONYMS, EXPLICIT_FLAVOR, CAPACITY_GENERATION,
                                  CATEGORY_CHAR_FIX, DECORATIONS, STRONG_NEW_MARKERS, STRONG_OLD_MARKERS)
    from contact_fields import LABELS
    rows = []
    def add(kind, alias, standard, category='', model=''):
        from alias_policy import QUARANTINED_TYPES, legacy_aliases_enabled
        key = json.dumps([kind, category, alias, standard], ensure_ascii=False)
        rows.append(dict(id='builtin-' + hashlib.sha256(key.encode()).hexdigest()[:16],
                         key=alias, type=kind, alias=alias, standard=standard,
                         category=category, model=model, mode='确定性词典映射',
                         source='彩虹仓内置', status=('quarantined' if kind in QUARANTINED_TYPES
                         and not legacy_aliases_enabled() else 'active'), updated_at=None,
                         readonly=True))
    for alias, standard in MODEL_ALIASES.items():
        add('MODEL_ALIASES', alias, standard, model=standard)
    for alias, values in FLAVOR_SYNONYMS.items():
        for standard in values:
            add('FLAVOR_SYNONYMS', alias, standard)
    for (category, alias), standard in EXPLICIT_FLAVOR.items():
        add('EXPLICIT_FLAVOR', alias, standard, category, category)
    for category, values in CAPACITY_GENERATION.items():
        for alias, standard in values.items():
            add('CAPACITY_GENERATION', alias, standard, category, category)
    for alias, standard in CATEGORY_CHAR_FIX.items():
        add('CATEGORY_CHAR_FIX', chr(alias), standard)
    for value in DECORATIONS:
        add('DECORATIONS', value, '型号装饰词（受代际保护规则约束）')
    for values, standard in ((STRONG_NEW_MARKERS, '新代标记'), (STRONG_OLD_MARKERS, '旧代标记')):
        for value in values:
            add('GENERATION_MARKERS', value, standard)
    for standard, aliases in LABELS.items():
        for alias in aliases:
            add('CONTACT_LABELS', alias, standard)
    # Display actual runtime constants/functions, not a separately maintained
    # approximation of what the parser might do.
    import inspect
    import parser_rules
    import contact_fields
    from order_parser import OrderParser
    from bot import OrderBot
    add('QUANTITY_UNITS','数量单位',parser_rules.QUANTITY_UNITS)
    for name in ('_LIST_MARKER_RE','_ORDER_TOTAL_RE','_SCOPED_TOTAL_RE','_BARE_TOTAL_RE','_ITEM_RE','_DELIVERY_HINT_RE'):
        add('SHARED_PATTERN',name,getattr(parser_rules,name).pattern)
        rows[-1]['source']='Shared Parser'
    for name in ('LABEL_RE','CORRECTION_RE'):
        add('CONTACT_PATTERN',name,getattr(contact_fields,name).pattern)
    for name,function in (('订单规范化/replacement',OrderParser._normalize_text),
                          ('目录规范化',OrderBot._normalize_catalog_token),
                          ('姓名校验',contact_fields.valid_name),
                          ('电话/地址提取',contact_fields.parse_contact)):
        add('RUNTIME_IMPLEMENTATION',name,inspect.getsource(function).strip())
    return rows


def rules():
    # Each entry points to an existing implementation; no executable data rules.
    specs = [
        ('normalizer', 'Shared Rules', 'parser_rules.py:normalize_order_text'),
        ('ITEM_QUANTITY', 'Shared Rules', 'parser_rules.py:_item_quantity'),
        ('SECTION_SUBTOTAL', 'Shared Rules', 'parser_rules.py:analyze_quantity_scopes'),
        ('ORDER_TOTAL', 'Shared Rules', 'parser_rules.py:quantity_declaration'),
        ('数量校验/防误识别', 'Shared Rules', 'parser_rules.py:validate_quantity_facts'),
        ('历史格式', '彩虹仓专属规则', 'order_parser.py:extract_order_info'),
        ('Contact', '彩虹仓专属规则', 'contact_fields.py:diagnose_contact'),
        ('地址', '彩虹仓专属规则', 'address_utils.py:normalize_cn_address'),
        ('代际防串货', '彩虹仓专属规则', 'deepseek_resolver.py:_raw_generation'),
        ('AI辅助/本地优先', '彩虹仓专属规则', 'bot.py:handle_message'),
        ('本地商品匹配', '彩虹仓专属规则', 'bot.py:OrderBot._match_product'),
        ('本地目录规范化', '彩虹仓专属规则', 'bot.py:OrderBot._normalize_catalog_token'),
        ('目录型号匹配', '彩虹仓专属规则', 'deepseek_resolver.py:resolve_category'),
        ('目录口味匹配', '彩虹仓专属规则', 'deepseek_resolver.py:resolve_flavor'),
    ]
    return [dict(id=name, group=group, implementation=ref, readonly=True,
                 source='Shared Parser' if group == 'Shared Rules' else '彩虹仓本地')
            for name, group, ref in specs]


def overview(filters=None):
    from parser_rules import SHARED_PARSER_RULESET_VERSION
    manifest_path = ROOT / 'parser_sync_manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    stored = read_store()
    all_rows = builtins() + stored['aliases']
    rows = all_rows
    for key, query in (filters or {}).items():
        if not query:
            continue
        if key == 'q':
            rows = [r for r in rows if str(query).casefold() in json.dumps(r, ensure_ascii=False).casefold()]
        elif key in ('type', 'source', 'status', 'category', 'model'):
            rows = [r for r in rows if r.get(key) == query]
    conflicts = []
    for row in all_rows:
        if row['type'] != 'FLAVOR_SYNONYMS' and row['status'] == 'active' and any(
            other['status'] == 'active' and other['id'] != row['id']
            and other['type'] == row['type'] and other['category'] == row['category']
            and other['alias'] == row['alias'] and other['standard'] != row['standard']
            for other in all_rows
        ):
            conflicts.append(row['id'])
    return dict(project='彩虹仓', shared_version=SHARED_PARSER_RULESET_VERSION,
                shared_hash=hashlib.sha256((ROOT / 'parser_rules.py').read_bytes()).hexdigest(),
                parser_version=manifest.get('project_parser_version', '未记录'),
                updated_at=stored.get('updated_at'),
                sync_source=manifest.get('last_sync_source', '未记录'),
                sync_at=manifest.get('last_sync_at'), rules=rules(), rows=rows,
                rule_count=len(rules()), dictionary_count=len(all_rows),
                pending_count=sum(r['status'] == 'pending' for r in all_rows),
                conflict_count=len(conflicts), conflicts=conflicts,
                review_candidates=review_candidates(),
                audit=stored.get('audit', [])[-30:])


def mutate(body, actor):
    if not isinstance(body, dict) or set(body) - {'action', 'id', 'alias', 'standard', 'category', 'status', 'type'}:
        raise ValueError('字段无效；仅支持人工分类内口味Alias')
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(str(target) + '.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        store = read_store()
        action = body.get('action')
        row_id = body.get('id')
        if row_id and str(row_id).startswith('builtin-'):
            raise ValueError('内置词典只读')
        row = next((r for r in store['aliases'] if r['id'] == row_id), None)
        if action == 'create':
            kind = body.get('type', 'EXPLICIT_FLAVOR')
            if kind not in ('EXPLICIT_FLAVOR', 'MODEL_ALIASES'):
                raise ValueError('人工Alias仅支持分类映射或分类内口味映射')
            row = dict(id='manual-' + secrets.token_hex(8), type=kind,
                       mode='分类内精确匹配', source='人工新增', readonly=False,
                       status='pending')
            store['aliases'].append(row)
        elif action not in ('update', 'delete') or row is None:
            raise ValueError('操作或人工Alias不存在')
        if action == 'delete':
            store['aliases'].remove(row)
        else:
            for field in ('alias', 'standard', 'category'):
                value = body.get(field, row.get(field))
                if not isinstance(value, str) or not value.strip() or len(value) > 120 or any(ord(c) < 32 for c in value):
                    raise ValueError('Alias、标准值与分类必须为1–120字符文本')
                row[field] = value.strip()
            row['model'] = row['category']
            if action == 'update' and any(k in body for k in ('alias', 'standard', 'category')):
                row['status'] = 'pending'
            if 'status' in body and action != 'create':
                if body['status'] not in ('active', 'pending', 'disabled'):
                    raise ValueError('状态无效')
                row['status'] = body['status']
            if any(r.get('status') in ('active','pending') and r['id'] != row['id'] and r['type'] == row['type']
                   and r['category'] == row['category'] and r['alias'] == row['alias']
                   for r in builtins() + store['aliases']):
                raise ValueError('Alias重复或冲突，不能覆盖内置映射')
            row['updated_at'] = datetime.now(timezone.utc).isoformat()
        store['updated_at'] = datetime.now(timezone.utc).isoformat()
        store['audit'] = (store.get('audit', []) + [dict(actor=actor, action=action,
                          id=row['id'], at=store['updated_at'])])[-200:]
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.dictionary-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(store, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return row


def manual_flavor(category, alias, pool, products=None):
    if alias in pool:
        return alias
    from brand_aliases import scope_matches
    store=runtime_store()
    from flavor_aliases import mapping_rejected
    candidates = [r for r in store['aliases'] if r['status'] == 'active'
                  and r['type'] == 'EXPLICIT_FLAVOR'
                  and r['category'] == category and r['alias'].casefold() == str(alias).casefold()
                  and (r.get('scope')=='GLOBAL' or not r.get('warehouse_id') and not r.get('category_id') or
                       products is not None and any(scope_matches(r,p) for p in products))
                  and not mapping_rejected(store,r)
                  ]
    standards = {r['standard'] for r in candidates}
    if len(standards) > 1:
        raise ValueError('当前分类存在冲突口味Alias，需管理员处理')
    if standards and not standards.issubset(set(pool)):
        raise ValueError('已批准口味Alias目标不在当前可售目录，需管理员核对，不能自动替换')
    return next(iter(standards)) if standards else None


def manual_model(alias, categories):
    candidates = [r for r in runtime_store()['aliases'] if r['status'] == 'active'
                  and r['type'] == 'MODEL_ALIASES' and r['alias'] == alias
                  and (r.get('scope')=='GLOBAL' or not r.get('category_id'))
                  and r['standard'] in categories]
    standards={r['standard'] for r in candidates}
    if len(standards)>1:raise ValueError('全后台存在冲突品牌Alias，需管理员处理')
    return next(iter(standards)) if standards else None


def review_candidates():
    # Audit conclusions only. Never consulted by runtime alias lookups.
    return [dict(id='audit-14-15',type='MODEL_ALIASES',alias='板砖 1w 口',
                 standard='板砖一代',category='板砖一代',status='pending',
                 source='历史同步',reason='样本14/15：容量限定，审核时必须保留代际保护'),
            dict(id='audit-78',type='EXPLICIT_FLAVOR',alias='黄瓜柠檬',
                 standard='青瓜柠檬水',category='冰爆',status='pending',
                 source='历史同步',reason='样本78：尚需人工确认语义等价')]
