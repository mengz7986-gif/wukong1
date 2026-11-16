"""Scoped shipping overrides. Missing overrides retain the existing default policy.

This file contains no credentials; readers in Bot and API use the same store.
No import-time writes or database migrations.
"""
import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path


def path():
    return Path(os.environ.get('RAINBOW_SHIPPING_PATH', str(Path(__file__).parent / 'data' / 'warehouse_shipping.json')))


def read():
    if not path().exists():
        return {'rules': {}, 'audit': []}
    result = json.loads(path().read_text(encoding='utf-8'))
    if not isinstance(result, dict) or not isinstance(result.get('rules'), dict):
        raise ValueError('仓库运费配置损坏，禁止猜测运费')
    return result


def save(warehouse_id, category_id, fee, units, actor, *, remove=False):
    try:
        amount = Decimal(str(fee))
        count = int(units)
        if isinstance(units, bool) or str(count) != str(units) or count < 1:
            raise ValueError('计费数量必须为正整数')
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
            raise ValueError('运费必须为非负金额，最多两位小数')
    except (InvalidOperation, TypeError):
        raise ValueError('运费或计费数量无效') from None
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(str(target) + '.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        store = read()
        key = f'{int(warehouse_id)}:{int(category_id)}'
        prior = store['rules'].get(key)
        if remove:
            store['rules'].pop(key, None)
        else:
            store['rules'][key] = dict(warehouse_id=int(warehouse_id), category_id=int(category_id),
                                      fee=str(amount), units=count)
        store['audit'].append(dict(actor=actor, at=datetime.now(timezone.utc).isoformat(),
                                   key=key, before=prior, after=store['rules'].get(key)))
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.shipping-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(store, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return store['rules'].get(key)


def partition(rows):
    """Return explicit fees + unconfigured rows, never silently swallow corruption."""
    rules = read()['rules']
    groups, remaining = {}, []
    for row in rows:
        product = row.get('product')
        warehouse = row.get('warehouse_id') or getattr(product, 'default_warehouse_id', None)
        category = row.get('parent_id') or row.get('category_id') or getattr(product, 'parent_id', None)
        key = f'{warehouse}:{category}'
        if key not in rules:
            remaining.append(row)
            continue
        groups[key] = groups.get(key, 0) + int(row['quantity'])
    fee = Decimal('0')
    for key, quantity in groups.items():
        rule = rules[key]
        fee += (Decimal(quantity) / Decimal(rule['units'])).to_integral_value(rounding=ROUND_CEILING) * Decimal(rule['fee'])
    return float(fee), remaining
