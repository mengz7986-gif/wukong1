"""Independent address/day admission slots; no inventory or ledger writes.

Existing release facts are read, never backfilled or reclassified. Cancellation
keeps the admission fact for audit, but a cancelled order no longer consumes
the live address/day quota.
"""
from sqlalchemy import text
import package_rules
from collections import OrderedDict
import hashlib
from threading import Lock


# Only deterministic normalization is shared. Quotas/statuses/recording facts
# are NEVER cached and remain authoritative inside the existing transaction.
_NORMALIZATION_LIMIT = 2048
_normalization_cache = OrderedDict()
_normalization_lock = Lock()
_normalization_epoch = None


class _NormalizationMap(dict):
    epoch = None


def _epoch():
    from region_index import get_region_index
    index=get_region_index()
    return (index, package_rules.address_key) if index is not None else None


def _normalized(raw, epoch):
    global _normalization_epoch
    if epoch is None or len(raw)>4096:
        return package_rules.address_key(raw)
    # Retain no raw source text as a global cache key; results are the existing
    # address identity, bounded in memory and never written to a new store.
    key=hashlib.sha256(raw.encode()).digest()
    with _normalization_lock:
        if _normalization_epoch!=epoch:
            _normalization_cache.clear();_normalization_epoch=epoch
        if key in _normalization_cache:
            _normalization_cache.move_to_end(key)
            return _normalization_cache[key]
    # Do not serialize customer CPU work under the cache lock.
    result=package_rules.address_key(raw)
    try:unchanged=_epoch()==epoch
    except Exception:unchanged=False
    if unchanged:
        with _normalization_lock:
            if _normalization_epoch==epoch:
                _normalization_cache[key]=result
                _normalization_cache.move_to_end(key)
                while len(_normalization_cache)>_NORMALIZATION_LIMIT:
                    _normalization_cache.popitem(last=False)
    return result

DDL = (
    'CREATE TABLE IF NOT EXISTS address_order_release_slots (order_id INTEGER PRIMARY KEY REFERENCES orders(id), day TEXT NOT NULL, address_key TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS ix_address_order_release_day ON address_order_release_slots(day,address_key)',
)


def ready(session):
    return bool(session.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='address_order_release_slots'")).scalar())


def _address_key(session,address):
    # Raw-address reuse is per session; pure normalized identities are bounded
    # across requests. An index replacement invalidates both levels.
    cache=session.info.setdefault('address_daily_normalization',_NormalizationMap())
    try:epoch=_epoch()
    except Exception:epoch=None
    if epoch is None:
        cache.clear()
        return package_rules.address_key(str(address or ''))
    cached_epoch=getattr(cache,'epoch',session.info.get('address_daily_normalization_epoch'))
    if cached_epoch!=epoch:
        cache.clear();session.info['address_daily_normalization_epoch']=epoch
        if isinstance(cache,_NormalizationMap):cache.epoch=epoch
    raw=str(address or '')
    if raw not in cache:cache[raw]=_normalized(raw,epoch)
    return cache[raw]


def prewarm(database, when, extra_addresses=()):
    """Pure normalization before the writer lock; never cache admission facts.

    A concurrent new/changed address is normalized on demand under the lock.
    All quotas, statuses and release rows are still read after BEGIN IMMEDIATE.
    """
    from models import Order
    start,end=package_rules.day_window_utc(when)
    with database.SessionLocal() as session:
        addresses={str(raw or '') for (raw,) in session.query(Order.recipient_address).filter(
            Order.status=='split_pending').all()}
        addresses.update(str(raw or '') for (raw,) in session.query(Order.recipient_address).filter(
            Order.created_at>=start,Order.created_at<end,
            Order.status.notin_(('cancelled','split_pending'))).all())
        if ready(session):
            addresses.update(str(raw or '') for (raw,) in session.execute(text(
                'SELECT o.recipient_address FROM split_release_days r JOIN orders o ON o.id=r.order_id WHERE r.day=:d'),
                {'d':package_rules.local_day_str(when)}).all())
        # A legacy predecessor can predate today while still lacking a known
        # completion clock. Warm a conservative address-only superset; the
        # original predecessor predicate still rereads live facts under lock.
        addresses.update(str(raw or '') for (raw,) in session.execute(text('''
            SELECT recipient_address FROM orders WHERE coalesce(status,'')!='cancelled'
            AND (recorded_at IS NULL OR julianday(recorded_at)>=julianday(:start)
                 OR julianday(fhd_last_push_at)>=julianday(:start))'''),
            {'start':start.isoformat(sep=' ')}).all())
    addresses.update(str(raw or '') for raw in extra_addresses)
    # Each lookup validates the live index identity. Do not keep stale results
    # if a region snapshot changes while this optional warmup is running.
    from types import SimpleNamespace
    scratch=SimpleNamespace(info={})
    for raw in addresses:_address_key(scratch,raw)
    return scratch.info.get('address_daily_normalization',_NormalizationMap())


def used(session, address, when, exclude=()):
    from models import Order
    key=_address_key(session,address)
    day=package_rules.local_day_str(when)
    found=set(session.execute(text('''SELECT s.order_id FROM address_order_release_slots s
        JOIN orders o ON o.id=s.order_id
        WHERE s.day=:d AND s.address_key=:a AND o.status!='cancelled' '''),
        {'d':day,'a':key}).scalars())
    # Legacy single/group admissions may precede activation. Read evidence,
    # but do not alter their status, group, timestamps or release history.
    legacy=session.execute(text("SELECT o.id,o.recipient_address FROM split_release_days r JOIN orders o ON o.id=r.order_id WHERE r.day=:d AND o.status!='cancelled'"),{'d':day}).all()
    found.update(oid for oid,raw in legacy if _address_key(session,raw)==key)
    start,end=package_rules.day_window_utc(when)
    prior=session.query(Order).filter(Order.created_at>=start,Order.created_at<end,
        Order.status.notin_(('cancelled','split_pending'))).all()
    found.update(o.id for o in prior if _address_key(session,o.recipient_address)==key)
    return len(found-set(exclude))


def record(session, order, when):
    session.execute(text('INSERT OR IGNORE INTO address_order_release_slots(order_id,day,address_key) VALUES(:o,:d,:a)'),
        {'o':order.id,'d':package_rules.local_day_str(when),'a':_address_key(session,order.recipient_address)})


def predecessor_blockers(session, order, when):
    """Shared clone-only address sequence, across groups/warehouses/days.

    Read authoritative rows under the caller's writer lock. Never cache
    admission or recording facts; the per-session cache is normalization only.
    Database IDs define creation order, not customer text/sequence numbers.
    Cancelled rows do not reserve cross-batch capacity. The existing stricter
    same-group cancellation/ordinal predicate remains in split_management.
    """
    from telegram_source_identity import account_cutover
    if account_cutover() is None:return []
    key=_address_key(session,order.recipient_address)
    if not key:return [dict(code='ADDRESS_IDENTITY_UNKNOWN',id=order.id)]
    from datetime import datetime,timezone
    def utc(value):
        if not isinstance(value,datetime):value=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    try:current=utc(when)
    except (TypeError,ValueError):return [dict(code='ADDRESS_CLOCK_UNKNOWN',id=order.id)]
    start,_=package_rules.day_window_utc(current)
    # Successful, specifically timed predecessors before today's boundary
    # cannot block. Filter those in SQL instead of re-normalizing all history.
    rows=session.execute(text('''SELECT id,status,recorded_at,fhd_push_status,
        fhd_last_push_at,recipient_address FROM orders
        WHERE id<:id AND coalesce(status,'')!='cancelled' AND (
          NOT (recorded_at IS NOT NULL OR coalesce(status,'') IN ('confirmed','recorded','shipped')
               OR coalesce(fhd_push_status,'')='success')
          OR julianday(coalesce(recorded_at,CASE WHEN fhd_push_status='success' THEN fhd_last_push_at END)) IS NULL
          OR julianday(coalesce(recorded_at,CASE WHEN fhd_push_status='success' THEN fhd_last_push_at END))>=julianday(:start)
        ) ORDER BY id'''),{'id':order.id,'start':start.isoformat(sep=' ')}).mappings().all()
    blockers=[]
    for row in rows:
        if _address_key(session,row['recipient_address'])!=key:continue
        positive=bool(row['recorded_at'] or row['status'] in ('confirmed','recorded','shipped') or row['fhd_push_status']=='success')
        if not positive:
            blockers.append(dict(code='ADDRESS_PREDECESSOR_NOT_RECORDED',id=row['id']));continue
        completed=row['recorded_at'] or (row['fhd_last_push_at'] if row['fhd_push_status']=='success' else None)
        if completed is None:
            blockers.append(dict(code='ADDRESS_PREDECESSOR_TIME_UNKNOWN',id=row['id']));continue
        try:completed=utc(completed)
        except (TypeError,ValueError):
            blockers.append(dict(code='ADDRESS_PREDECESSOR_TIME_UNKNOWN',id=row['id']));continue
        if package_rules.local_day_str(completed)>=package_rules.local_day_str(current):
            blockers.append(dict(code='ADDRESS_PREDECESSOR_NEXT_DAY',id=row['id']))
    return blockers


def predecessor_ready(session,order,when):
    return not predecessor_blockers(session,order,when)


def restored_status(session, order, when, requested):
    """Re-admit one cancelled order under the current address/day capacity.

    The caller must hold the same writer lock used for stock/FIFO restoration.
    A prior admission row is audit history, not a permanent reservation.
    """
    import config
    config.load_runtime_settings()
    cap=int(getattr(config,'ADDRESS_DAILY_MAX_ORDERS',0) or 0)
    if not cap:
        return requested
    if not ready(session):
        raise ValueError('同地址每日订单数Migration未完成，暂不恢复订单')
    if (used(session,order.recipient_address,when,exclude=(order.id,))>=cap
            or not predecessor_ready(session,order,when)):
        if not order.split_group_id:
            order.split_group_id='daily:'+str(order.id)
            order.split_index=order.split_count=1
        return 'split_pending'
    record(session,order,when)
    return requested


def release_cancelled(session, order, when):
    """Fill a genuinely released daily slot with the oldest eligible waiter."""
    import config
    config.load_runtime_settings()
    cap=int(getattr(config,'ADDRESS_DAILY_MAX_ORDERS',0) or 0)
    if not cap or not ready(session):
        return []
    session.flush()  # make the cancelled status visible to the SQL audit join
    available=max(0,cap-used(session,order.recipient_address,when))
    if not available:
        return []
    from models import Order
    key=_address_key(session,order.recipient_address)
    start,end=package_rules.day_window_utc(when)
    candidates=session.query(Order).filter(
        Order.created_at>=start,Order.created_at<end,
        Order.status=='split_pending',Order.split_group_id.like('daily:%'),
    ).order_by(Order.created_at,Order.id).all()
    released=[]
    from split_management import record_release
    for candidate in candidates:
        if available<=0:break
        if _address_key(session,candidate.recipient_address)!=key:continue
        if not predecessor_ready(session,candidate,when):continue
        candidate.status='pending'
        record(session,candidate,when)
        record_release(session,candidate.split_group_id,candidate,when)
        released.append(candidate.id);available-=1
    return released


def apply_created(session, created, when, cap, unit_cap=0):
    if not cap:return
    if not ready(session):raise ValueError('同地址每日订单数Migration未完成，暂不接受新订单')
    excluded={o.id for o in created}
    available=max(0,cap-used(session,created[0].recipient_address,when,excluded))
    from split_management import processed
    for order in sorted(created,key=lambda o:(o.created_at,o.id)):
        if order.status=='cancelled' or processed(order):continue
        # The independent order cap never relaxes an enabled unit cap.
        if (available and (order.status=='pending' or not unit_cap)
                and predecessor_ready(session,order,when)):
            order.status='pending';record(session,order,when);available-=1
        else:
            order.status='split_pending'
            if not order.split_group_id:
                order.split_group_id='daily:'+str(order.id)
                order.split_index=order.split_count=1
