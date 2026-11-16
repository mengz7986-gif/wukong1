"""Dedicated release admission only. No Telegram, orders, migrations or seeding.

Reuses split_management.tick. Existing scheduler transaction owns transitions;
the SQLite authorizer is an additional hard business-write boundary.
"""
import hashlib,json,os,re,sqlite3,stat
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace

def timestamp(value):
    parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('SPLIT_POLICY_TIMEZONE_REQUIRED')
    return parsed.astimezone(timezone.utc)

def validate_policy(policy,*,bot_id,database,boundary,now=None,require_enabled=True):
    if policy.get('version')!=1 or type(policy.get('enabled')) is not bool:
        raise ValueError('SPLIT_POLICY_FORMAT')
    if require_enabled and not policy['enabled']:raise ValueError('SPLIT_POLICY_DISABLED')
    if not str(policy.get('bot_id','')).isdigit() or str(policy.get('bot_id'))!=str(bot_id):raise ValueError('SPLIT_POLICY_IDENTITY')
    if Path(policy.get('database','')).resolve()!=Path(database).resolve():raise ValueError('SPLIT_POLICY_DATABASE')
    if timestamp(policy.get('account_boundary'))!=timestamp(boundary):raise ValueError('SPLIT_POLICY_BOUNDARY')
    activation=timestamp(policy.get('activated_at'))
    if activation<timestamp(boundary) or activation>(now or datetime.now(timezone.utc)):
        raise ValueError('SPLIT_POLICY_ACTIVATION')
    frontier=policy.get('historical_max_order_id')
    if type(frontier) is not int or frontier<0:raise ValueError('SPLIT_POLICY_FRONTIER')
    approved=policy.get('approved_backlog')
    if not isinstance(approved,dict) or any(not re.fullmatch(r'[1-9][0-9]*',str(k))
        or int(k)>frontier or not re.fullmatch('[0-9a-f]{64}',str(v)) for k,v in approved.items()):
        raise ValueError('SPLIT_POLICY_BACKLOG')
    if policy.get('notifications_enabled') is not False:raise ValueError('SPLIT_NOTIFICATIONS_FORBIDDEN')
    return policy

def load_policy(path,expected_sha256,**context):
    path=Path(path)
    if path.is_symlink():raise ValueError('SPLIT_POLICY_SYMLINK')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o022 or info.st_uid not in (0,os.getuid()):
            raise ValueError('SPLIT_POLICY_PERMISSIONS')
        with os.fdopen(fd,'rb',closefd=False) as f:raw=f.read(1024*1024+1)
        if len(raw)>1024*1024 or hashlib.sha256(raw).hexdigest()!=expected_sha256:
            raise ValueError('SPLIT_POLICY_HASH')
        return validate_policy(json.loads(raw),**context)
    finally:os.close(fd)

def review_fingerprint(order,members):
    """Opaque review identity includes items and address, never exports either."""
    from split_management import historical_release_fingerprint
    details=[]
    for row in members:
        items=sorted((int(i.product_id),int(i.warehouse_id),int(i.quantity),str(i.unit_price)) for i in row.items)
        details.append(dict(id=row.id,address_hash=hashlib.sha256(
            str(row.recipient_address or '').encode()).hexdigest(),items=items))
    raw=json.dumps(dict(state=historical_release_fingerprint(order,members),details=details),
        sort_keys=True,separators=(',',':')).encode()
    return hashlib.sha256(raw).hexdigest()

def release_allowed(policy,order,members):
    created=order.created_at
    if created is None:return False
    if created.tzinfo is None:created=created.replace(tzinfo=timezone.utc)
    if created<timestamp(policy['account_boundary']):return False
    if order.id<=policy['historical_max_order_id']:
        return policy['approved_backlog'].get(str(order.id))==review_fingerprint(order,members)
    return created>=timestamp(policy['activated_at'])

def child_fits_current_parcel(order):
    """Do not auto-admit a stale oversized/cross-parcel child after rule drift."""
    from parcel_shipping import plan
    rows=[dict(product=i.product,product_id=i.product_id,warehouse_id=i.warehouse_id,
        quantity=i.quantity) for i in order.items]
    if not rows or any(type(r['quantity']) is not int or r['quantity']<=0 for r in rows):return False
    return len(plan(rows))==1

def authorize(action,table,column,database,trigger):
    # No orders INSERT/DELETE, no stock/FIFO/payments/prices or schema changes.
    if trigger and action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE):
        return sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE):
        # SQLAlchemy Order.updated_at has an existing onupdate clock. Allow
        # only that ordinary status-touch clock, never recorded_at/money.
        allowed=(action==sqlite3.SQLITE_UPDATE and table=='orders' and column in ('status','updated_at')) or (
            action==sqlite3.SQLITE_INSERT and table in ('split_release_days','address_order_release_slots'))
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
    forbidden=(sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,sqlite3.SQLITE_DROP_INDEX,sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_DROP_TRIGGER,sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH)
    if action in forbidden:return sqlite3.SQLITE_DENY
    if action==sqlite3.SQLITE_PRAGMA and column is not None:return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK

def database_adapter(path):
    """Open existing DB only; no Database constructor and no create_all."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool
    path=Path(path).resolve()
    if not path.is_file():raise ValueError('SPLIT_DATABASE_MISSING')
    def connect():
        c=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=.25,check_same_thread=False)
        c.execute('PRAGMA foreign_keys=ON')
        c.set_authorizer(authorize)
        return c
    engine=create_engine('sqlite://',creator=connect,poolclass=NullPool)
    return SimpleNamespace(SessionLocal=sessionmaker(bind=engine),engine=engine)

def dispatch_once(database,policy):
    from split_management import tick
    return tick(database,release_guard=lambda row,members:(
        release_allowed(policy,row,members) and child_fits_current_parcel(row)))

def try_dispatch_once(database,policy):
    """Transient SQLite writer contention yields, not a dead worker or replay."""
    from sqlalchemy.exc import OperationalError
    try:return dict(state='completed',released=dispatch_once(database,policy))
    except OperationalError as error:
        original=error.orig
        code=getattr(original,'sqlite_errorcode',None)
        # SQLite primary codes BUSY=5 / LOCKED=6; Python 3.10 does not export
        # the named constants/exception attribute on every supported build.
        primary=(code & 0xff) if isinstance(code,int) else None
        if isinstance(original,sqlite3.OperationalError) and (
                primary in (5,6)
                or str(original) in ('database is locked','database table is locked')):
            return dict(state='busy',released=[])
        raise
