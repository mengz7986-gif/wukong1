"""Reviewed aliases target real bundle IDs; never create catalog products.

Legacy bundle names stay in their original table. Explicit dictionary overrides
can retire/edit a legacy alias without altering bundle configuration or history.
"""
import hashlib
import secrets
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
import rule_dictionary as dictionary
from brand_aliases import clean_alias, transaction


def key(value):
    from bundles import normalized
    return normalized(str(value))


def reserved_names():
    """Read-only canonical/legacy bundle identities for brand conflict checks."""
    url=os.getenv('DATABASE_URL','sqlite:///./orders.db')
    if not url.startswith('sqlite:///'): return set()
    path=Path(url[len('sqlite:///'):]).resolve()
    if not path.is_file(): return set()
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='bundle_names'").fetchone(): return set()
        return {key(r[0]) for r in connection.execute('SELECT display_name FROM bundle_names')}


def rows(bundle, store):
    saved = [r for r in store.get('aliases', [])
             if r.get('type') == 'BUNDLE_ALIASES' and r.get('bundle_id') == bundle['id']]
    overrides = {r.get('legacy_alias') for r in saved if r.get('legacy_alias')}
    legacy = [dict(id='legacy-bundle-' + str(bundle['id']) + '-' + hashlib.sha256(a.encode()).hexdigest()[:16],
        type='BUNDLE_ALIASES', bundle_id=bundle['id'], alias=a, standard=bundle['name'],
        status='active', source='既有福袋配置', legacy_alias=a, readonly=False)
        for a in bundle.get('aliases', []) if a not in overrides]
    return legacy + [dict(r, standard=bundle['name']) for r in saved if r.get('status') != 'deleted']


def effective(bundle, store=None):
    store = dictionary.runtime_store() if store is None else store
    return [r['alias'] for r in rows(bundle, store) if r.get('status') == 'active']


def groups(catalog, store):
    return [dict(type='bundle', bundle_id=b['id'], standard=b['name'],
        category_id=None, warehouse_id=None, warehouses=[], products=[], flavors=[],
        sellable=b.get('sellable', False), aliases=rows(b, store),
        management_category_id=b['category_id']) for b in catalog]


def check_conflict(alias, bundle, catalog, products, store, exclude=None):
    wanted = key(alias)
    if wanted in {key(p.get('name', '')) for p in products} | {key(p.get('parent_name', '')) for p in products}:
        raise ValueError('叫法与正式品牌或商品名称冲突')
    for other in catalog:
        if wanted == key(other['name']):
            raise ValueError('叫法与福袋标准名称冲突')
        for row in rows(other, store):
            if row['id'] == exclude: continue
            if row.get('status') in ('active', 'pending') and key(row['alias']) == wanted:
                raise ValueError('叫法与已有福袋叫法重复或冲突')
    for row in store.get('aliases', []) + dictionary.builtins():
        if row.get('type') != 'BUNDLE_ALIASES' and row.get('status') in ('active', 'pending') and key(row['alias']) == wanted:
            raise ValueError('叫法与正式品牌或口味叫法冲突')


def mutate(body, actor, catalog, products):
    bundle = next((b for b in catalog if b['id'] == int(body.get('bundle_id', 0))), None)
    if not bundle: raise ValueError('福袋不存在或已退役')
    action = body.get('action')
    if action not in ('create', 'edit', 'approve', 'disable', 'delete'):
        raise ValueError('叫法操作无效')
    def change(store):
        now = datetime.now(timezone.utc).isoformat()
        if action == 'create':
            lines = str(body.get('aliases', '')).splitlines()
            aliases = [clean_alias(a) for a in lines if a.strip()]
            if not 1 <= len(aliases) <= 200: raise ValueError('请填写1–200条叫法')
            if len({key(a) for a in aliases}) != len(aliases): raise ValueError('输入叫法重复')
            for alias in aliases: check_conflict(alias, bundle, catalog, products, store)
            targets = [dict(id='bundle-alias-' + secrets.token_hex(8), type='BUNDLE_ALIASES',
                bundle_id=bundle['id'], alias=a, standard=bundle['name'], status='pending',
                source='人工维护福袋叫法', readonly=False, created_by=actor, created_at=now,
                reviewed_by=None, reviewed_at=None) for a in aliases]
            store['aliases'].extend(targets)
        else:
            current = next((r for r in rows(bundle, store) if r['id'] == body.get('id')), None)
            if not current: raise ValueError('叫法不存在或已删除，请刷新')
            current = dict(current)
            if action == 'edit':
                alias = clean_alias(body.get('alias'))
                check_conflict(alias, bundle, catalog, products, store, current['id'])
                current.update(alias=alias, status='pending', reviewed_by=None, reviewed_at=None)
            elif action == 'approve':
                check_conflict(current['alias'], bundle, catalog, products, store, current['id'])
                current.update(status='active', reviewed_by=actor, reviewed_at=now)
            else: current['status'] = 'disabled' if action == 'disable' else 'deleted'
            store['aliases'] = [r for r in store['aliases'] if r.get('id') != current['id']]
            store['aliases'].append(current)
            targets = [current]
        for row in targets:
            row['updated_at'] = now
            store['audit'].append(dict(actor=actor, action='bundle_alias_' + action,
                id=row['id'], bundle_id=bundle['id'], at=now))
        store['updated_at'] = now
        return dict(success=True, changed=len(targets), status=targets[0]['status'])
    return transaction(change)
