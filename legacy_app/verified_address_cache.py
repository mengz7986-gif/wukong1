"""Post-commit, user-isolated encrypted address hints. Never auto-applied."""
import hashlib,hmac,json,os,sqlite3,time
from pathlib import Path
from cryptography.fernet import Fernet,InvalidToken
from offline_address import assess

TTL_SECONDS=30*86400

def _settings():
    path=os.getenv('RAINBOW_VERIFIED_ADDRESS_CACHE_PATH')
    key=os.getenv('AI_SETTINGS_MASTER_KEY')
    if not path or not key:return None
    try:return Path(path),Fernet(key.encode()),key.encode()
    except (ValueError,TypeError):return None

def _token(secret,*parts):
    return hmac.new(secret,json.dumps(parts,ensure_ascii=False,separators=(',',':')).encode(),hashlib.sha256).hexdigest()

def _compact(value):return ''.join(str(value or '').split()).casefold()

def remember_committed(preview,order,commit_fingerprint):
    cfg=_settings()
    principal=preview.get('telegram_id')
    if not cfg or principal is None or not commit_fingerprint:return False
    if not isinstance(order,dict) or not (order.get('id') or order.get('order_number') or order.get('order_numbers')):return False
    address=preview.get('recipient_address') or preview.get('address')
    if order.get('recipient_address') and _compact(order['recipient_address'])!=_compact(address):return False
    decision=assess(address)
    if not decision['valid']:return False
    path,cipher,secret=cfg
    path.parent.mkdir(parents=True,exist_ok=True)
    owner=_token(secret,'owner',str(principal));version=decision['snapshot_id']
    normalized=decision['normalized'];detail=decision['meta']['detail']
    identity=_token(secret,'identity',owner,normalized)
    payload=cipher.encrypt(json.dumps(dict(address=normalized,meta=decision['meta']),ensure_ascii=False).encode()).decode()
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);os.fchmod(fd,0o600);os.close(fd)
    with sqlite3.connect(str(path),timeout=0.3) as db:
        db.execute('CREATE TABLE IF NOT EXISTS verified_addresses (owner TEXT,identity TEXT,full_key TEXT,detail_key TEXT,version TEXT,verified_at REAL,payload TEXT,PRIMARY KEY(owner,identity))')
        db.execute('DELETE FROM verified_addresses WHERE verified_at<?',(time.time()-TTL_SECONDS,))
        db.execute('INSERT OR REPLACE INTO verified_addresses VALUES(?,?,?,?,?,?,?)',
            (owner,identity,_token(secret,'lookup',owner,_compact(normalized)),_token(secret,'lookup',owner,_compact(detail)),version,time.time(),payload))
        db.execute('DELETE FROM verified_addresses WHERE owner=? AND rowid NOT IN (SELECT rowid FROM verified_addresses WHERE owner=? ORDER BY verified_at DESC LIMIT 500)',(owner,owner))
        db.execute('DELETE FROM verified_addresses WHERE rowid NOT IN (SELECT rowid FROM verified_addresses ORDER BY verified_at DESC LIMIT 10000)')
    return True

def suggestions(principal,raw):
    """Read only. A historical address is always a candidate, never proof."""
    cfg=_settings()
    if not cfg or principal is None:return []
    path,cipher,secret=cfg
    if not path.exists():return []
    current=assess(raw);meta=current['meta']
    owner=_token(secret,'owner',str(principal))
    keys={_token(secret,'lookup',owner,_compact(v)) for v in (raw,meta.get('detail')) if v}
    if not keys:return []
    try:
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0.2) as db:
            rows=db.execute('SELECT payload FROM verified_addresses WHERE owner=? AND version=? AND verified_at>=? AND (full_key IN ('+','.join('?' for _ in keys)+') OR detail_key IN ('+','.join('?' for _ in keys)+')) ORDER BY verified_at DESC LIMIT 4',
                (owner,current['snapshot_id'],time.time()-TTL_SECONDS,*keys,*keys)).fetchall()
        results=[]
        for (payload,) in rows:
            value=json.loads(cipher.decrypt(payload.encode()))
            if any(meta.get(k) and value['meta'].get(k)!=meta[k] for k in ('province','city','district')):continue
            if not assess(value['address'])['valid']:continue
            if value['address'] not in results:results.append(value['address'])
        return results
    except (sqlite3.Error,InvalidToken,ValueError,KeyError,TypeError):return []
