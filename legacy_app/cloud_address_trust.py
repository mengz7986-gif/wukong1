"""Server-issued, exact-address attestations shared by bot and persistence guards.

Only hashes are stored. No customer text or cloud credentials are retained here.
Pending proposals are not trusted until the owned draft is explicitly confirmed.
"""
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

class AddressTrustStorageError(RuntimeError):
    """Fixed diagnostic; never contains customer address or filesystem path."""


def _digest(address):
    return hashlib.sha256(''.join(str(address or '').split()).encode()).hexdigest()


@contextmanager
def _connect():
    path = Path(os.getenv('RAINBOW_CLOUD_ADDRESS_TRUST_PATH') or
                Path(__file__).resolve().parents[1] / 'work/legacy-data/cloud-address-trust.db')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS trusted (digest TEXT PRIMARY KEY, issued_at REAL NOT NULL)')
        with connection:yield connection
    finally:connection.close()


def remember(address):
    import time
    try:
        with _connect() as connection:
            connection.execute('INSERT OR REPLACE INTO trusted VALUES (?, ?)', (_digest(address), time.time()))
    except (OSError,sqlite3.Error):
        raise AddressTrustStorageError('ADDRESS_TRUST_STORAGE_UNAVAILABLE') from None


def trusted(address):
    import time
    try:
        with _connect() as connection:
            return connection.execute('SELECT 1 FROM trusted WHERE digest=? AND issued_at>?',
                                      (_digest(address), time.time()-86400)).fetchone() is not None
    except (OSError, sqlite3.Error):
        return False


def safety(raw, address, fields):
    """Preserve the original delivery span; never trust cloud-generated detail."""
    import re
    from address_admin_equivalence import same
    source = ''.join(str(raw or '').split())
    target = ''.join(str(address or '').split())
    if not source or re.search(r'1[3-9]\d{9}|收件|手机|电话|商品|口味', target):
        return False
    if not all(fields.get(k) for k in ('province', 'city', 'district')):
        return False
    # A known explicit province/city conflict is safety evidence, not a
    # requirement that the candidate's district exist in the local snapshot.
    from address_utils import normalize_cn_address
    original = normalize_cn_address(source)
    for key in ('province', 'city'):
        value = original.get(key)
        if value and value in source and not same(key, value, fields[key]):
            return False
    remainder=source
    preserved=target
    for key in ('province','city','district'):
        for value in (fields.get(key),original.get(key)):
            if value:
                remainder=remainder.replace(value,'')
                preserved=preserved.replace(value,'')
    if remainder not in preserved:return False
    detail = source
    for key in ('province', 'city', 'district', 'town'):
        value = fields.get(key)
        if value:
            detail = detail.replace(value, '')
    return bool(re.search(r'[\u4e00-\u9fff]{2,}', detail) and
                re.search(r'路|街|巷|村|苑|园|楼|栋|号|室|店|公司|大厦|小区|学校|医院', detail))
