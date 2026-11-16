"""Split queue only. Orders remain the sole stock, FIFO and ledger facts.

DDL is explicit, additive and rollback-compatible. No schema change on import.
Telegram delivery uncertainty never causes a business retry or blind resend.
"""
import fcntl
import hashlib
import json
import os
import uuid
from datetime import datetime
from sqlalchemy import text
import package_rules

DDL = (
    'CREATE TABLE IF NOT EXISTS split_release_days (group_id TEXT NOT NULL, day TEXT NOT NULL, order_id INTEGER NOT NULL REFERENCES orders(id), PRIMARY KEY(group_id,day))',
    '''CREATE TABLE IF NOT EXISTS split_waiting_messages (
       order_id INTEGER PRIMARY KEY REFERENCES orders(id), chat_id TEXT NOT NULL,
       message_id INTEGER, status TEXT NOT NULL DEFAULT 'pending', payload TEXT NOT NULL, sent_payload TEXT,
       last_error TEXT, updated_at TEXT NOT NULL)''',
)


def migrate(connection):
    for sql in DDL:
        connection.exec_driver_sql(sql)
    from address_daily_orders import DDL as address_ddl
    for sql in address_ddl:
        connection.exec_driver_sql(sql)


def rollback(connection):
    # Never erase sent-message identity or release facts after real usage.
    for table in ('split_waiting_messages', 'split_release_days', 'address_order_release_slots'):
        if not connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).scalar():continue
        if connection.exec_driver_sql('SELECT COUNT(*) FROM '+table).scalar():
            raise ValueError('分单管理已有业务事实；回退兼容代码并保留附加表，禁止删除')
    for table in ('split_waiting_messages', 'split_release_days', 'address_order_release_slots'):
        connection.exec_driver_sql('DROP TABLE IF EXISTS '+table)


def ready(session):
    return session.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('split_release_days','split_waiting_messages')")).scalar() == 2


def initialize_existing(database,now=None):
    """Explicit release migration: queue only, no stock or ledger changes."""
    from models import Order
    now=now or datetime.utcnow();changed=[]
    with database.SessionLocal() as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        groups={o.split_group_id for o in session.query(Order).filter(Order.split_group_id.isnot(None)).all()}
        for g in groups:
            if session.execute(text('SELECT 1 FROM split_release_days WHERE group_id=:g LIMIT 1'),{'g':g}).scalar():continue
            members=session.query(Order).filter(Order.split_group_id==g).order_by(Order.split_index,Order.id).all()
            pending=[o for o in members if o.status in ('pending','split_pending') and not processed(o)]
            if not pending:continue
            first=pending[0];first.status='pending';record_release(session,g,first,now)
            for o in pending[1:]:o.status='split_pending';changed.append(o.id)
        session.commit()
    return changed


def processed(order):
    # Attempt counts and partial/unknown external results are not positive
    # recording facts. Keep queue/progress predicates aligned with list SQL.
    return _recording_succeeded(order)


def _recording_uncertain(order):
    state=str(order.fhd_push_status or '')
    # Even a definitive failed attempt needs the existing explicit retry/review
    # path. Admission must not reset attempts or bypass pending-list filters.
    return state in ('sending','unknown','partial') or (
        int(order.fhd_push_count or 0)>0 and state!='success')


def historical_release_fingerprint(order,members):
    fields=('id','split_group_id','split_index','split_count','status','created_at',
            'recorded_at','recorded_export_batch_id','fhd_push_count','fhd_push_status')
    def snapshot(row):
        values={key:getattr(row,key,None) for key in fields}
        return {key:value.isoformat() if isinstance(value,datetime) else value for key,value in values.items()}
    value=dict(order_id=order.id,members=[snapshot(row) for row in members])
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _recording_succeeded(order):
    if order.status == 'cancelled':
        return False
    if getattr(order,'recorded_at',None) or order.status in ('confirmed','recorded','shipped'):
        return True
    state=str(order.fhd_push_status or '')
    # An attempt count is not evidence of successful recording.
    return state == 'success'


def recording_success_expression():
    """SQL counterpart of the positive recording fact; no inferred sends."""
    from sqlalchemy import and_, or_, func
    from models import Order
    return and_(Order.status != 'cancelled', or_(
        Order.recorded_at.is_not(None),
        Order.status.in_(['confirmed', 'recorded', 'shipped']),
        func.coalesce(Order.fhd_push_status, '') == 'success',
    ))


def planned_day(order,members):
    """Ordinal Beijing date; no persisted planned-date field exists."""
    from datetime import timedelta
    try:
        indexes=[int(row.split_index) for row in members]
        if indexes!=list(range(1,len(members)+1)) or any(int(row.split_count)!=len(members) for row in members):
            return None
        origin=min(row.created_at for row in members)
        return (datetime.fromisoformat(day(origin))+timedelta(days=int(order.split_index)-1)).date().isoformat()
    except (ValueError,TypeError,AttributeError):
        return None


def _eligible_release(session,candidate,members,now,approved_historical_release,release_guard=None):
    from telegram_source_identity import account_cutover
    boundary=account_cutover()
    if boundary is not None:
        from datetime import timezone
        created=getattr(candidate,'created_at',None)
        if created is None:return False
        if created.tzinfo is None:created=created.replace(tzinfo=timezone.utc)
        if created<boundary:return False
    # A pending external recording outcome requires review, not another
    # automatic admission. Failed attempts are not success or retry permission.
    if _recording_uncertain(candidate):return False
    from address_daily_orders import predecessor_ready
    if not predecessor_ready(session,candidate,now):return False
    # The dedicated clone worker supplies its frozen identity/backlog guard.
    # Run it inside this same writer transaction, after reading live members.
    if release_guard is not None and not release_guard(candidate,members):return False
    planned=planned_day(candidate,members)
    if planned is None or planned>day(now):return False
    for predecessor in members[:members.index(candidate)]:
        if not _recording_succeeded(predecessor):
            return False
        # A late recording starts a fresh natural-day boundary. Missing event
        # time cannot be invented from updated_at (which also tracks edits).
        completed = getattr(predecessor, 'recorded_at', None)
        if completed is None and str(predecessor.fhd_push_status or '') == 'success':
            # This clock is written by the existing successful FHD attempt
            # path. Unlike updated_at it is a specific external-record event.
            completed = getattr(predecessor, 'fhd_last_push_at', None)
        if completed is None or day(completed) >= day(now):
            return False
    if session.execute(text('SELECT 1 FROM split_release_days WHERE order_id=:id LIMIT 1'),{'id':candidate.id}).scalar():return False
    # Exact IDs from the original deployment are not identity in this clone.
    # Preserve the legacy gate only when no new-account boundary is configured.
    if boundary is None and candidate.id in HISTORICAL_REVIEW_ORDER_IDS:
        if (approved_historical_release or {}).get(candidate.id)!=historical_release_fingerprint(candidate,members):return False
    return True


def pending_date_expression():
    """Release natural-day basis for pending split rows, not creation day.

    Historical facts have DAY precision only, not a recovered event clock.
    Ordinary orders and absent release facts retain their original timestamp.
    """
    from sqlalchemy import select,func,column,case,table
    from models import Order
    releases=table('split_release_days',column('day'),column('order_id'))
    released=select(func.min(releases.c.day)).select_from(releases).where(
        releases.c.order_id==Order.id).correlate(Order).scalar_subquery()
    at=func.strftime('%Y-%m-%d %H:%M:%S.000000',released,'-8 hours')
    return case((Order.split_group_id.isnot(None),func.coalesce(at,Order.created_at)),else_=Order.created_at)


# Existing historical waiters require separate exact-ID review. New regular
# dispatch must not silently recover this backlog after a future deployment.
HISTORICAL_REVIEW_SNAPSHOT_SHA256 = 'fccd35bd818f65f64ce0a6ae4d0634e1559f9957ed91649149ff0360627a6c4a'
HISTORICAL_REVIEW_ORDER_IDS = frozenset((
    339,340,341,342,1541,3376,3803,3804,3805,3806,4010,4011,
    4257,4391,4392,4393,4394,4405,4960,5086,5087,5088,5102,5143,
    5194,5209,5268,5292,5298,5302,5307,5316,5317,5318,5371,5391,
    5438,5446,5465,5472,5480,5490,5495,5508,5509,5541,5545,5553,
    5554,5558,5692,5702,5739,5741,5743,5773,5774,5775,5932,5951,
    5952,5967,5986,5987,5991,5995,6002,6009,6010,6023,6069,6079,
    6082,6108,6113,6140,6141,6148,6149,6155,6172,6191,6374,6411,
    6415,6454,6461,6473,6490,6529,6545,6550,6586,6593,6599,6614,
    6619,6621,6654,6675,6717,6736,6752,6753,6755,6762,6781,6782,
    6812,6862,6919,7019,7074,7077,7082,7119,7192,7198,7201,7225,
    7245,7259,7280,7281,7282,7285,7292,7334,7335,7341,7342,7354,
    7355,7356,7363,7364,7394,7399,7400,7424,7441,7442,7477,7479,
    7510,7516,7525,7581,7628,7675,7700,7704,7717,7747,7780,7829,
    7836,7846,7858,7863,7874,7875,7892,7902,
))


def day(now=None):
    return package_rules.local_day_str(now or datetime.utcnow())


def summary(orders):
    done=[o for o in orders if o.status!='cancelled' and processed(o)]
    latest=max((o.updated_at or o.created_at for o in done),default=None)
    date=package_rules.local_day_str(latest or datetime.utcnow())
    label=f'{int(date[5:7])}.{int(date[8:10])}'
    return {'processed':len(done),'remaining':sum(o.status!='cancelled' and not processed(o) for o in orders),
            'progress':label+' '+f'已发{len(done)}单'}


def renumber(orders,group_id):
    # Keep cancelled positions: 2/5 cancellation must never become a new 2/4.
    for i,o in enumerate(sorted(orders,key=lambda o:(o.created_at,o.id)),1):
        o.split_group_id=group_id;o.split_index=i;o.split_count=len(orders)


def record_release(session,group_id,order,when):
    session.execute(text('INSERT OR IGNORE INTO split_release_days(group_id,day,order_id) VALUES(:g,:day,:id)'),
                    {'g':group_id,'day':day(when),'id':order.id})


def attach_created(session,created,now=None):
    import config
    config.load_runtime_settings()
    _validate_newbot_daily_policy(config)
    now=now or datetime.utcnow()
    _attach_created_legacy(session,created,now)
    from address_daily_orders import apply_created
    apply_created(session,created,now,int(getattr(config,'ADDRESS_DAILY_MAX_ORDERS',0) or 0),
        int(getattr(config,'ADDRESS_DAILY_MAX_UNITS',0) or 0))


def _attach_created_legacy(session,created,now=None):
    """Called inside the original order-creation transaction, never after commit."""
    from models import Order
    if not ready(session):
        raise ValueError('分单管理Migration未完成，暂不接受新订单')
    import config
    config.load_runtime_settings()
    now=now or datetime.utcnow()
    group_id=created[0].split_group_id
    members=list(created)
    cap=int(getattr(config,'ADDRESS_DAILY_MAX_UNITS',0) or 0)
    if cap:
        start,end=package_rules.day_window_utc(now)
        key=package_rules.address_key(created[0].recipient_address)
        candidates=session.query(Order).filter(Order.created_at>=start,Order.created_at<end).all()
        same=[o for o in candidates if o.status!='cancelled' and package_rules.address_key(o.recipient_address)==key]
        open_ids={o.split_group_id for o in session.query(Order).filter(Order.status=='split_pending').all()
                  if package_rules.address_key(o.recipient_address)==key and o.split_group_id}
        if sum(package_rules.total_units(o.items) for o in same)>cap or open_ids:
            ids={o.id for o in same}
            if open_ids:
                ids.update(o.id for o in session.query(Order).filter(Order.split_group_id.in_(open_ids)).all())
            # When two existing package groups merge, include every child.
            old_groups={o.split_group_id for o in same if o.split_group_id}|open_ids
            if old_groups:
                ids.update(o.id for o in session.query(Order).filter(Order.split_group_id.in_(old_groups)).all())
            members=session.query(Order).filter(Order.id.in_(ids)).order_by(Order.created_at,Order.id).all()
            group_id='address:'+str(min(ids))
            for old in old_groups:
                for row in session.execute(text('SELECT day,order_id FROM split_release_days WHERE group_id=:g'),{'g':old}).mappings().all():
                    session.execute(text('INSERT OR IGNORE INTO split_release_days(group_id,day,order_id) VALUES(:g,:d,:o)'),{'g':group_id,'d':row['day'],'o':row['order_id']})
    if len(members)<2:return
    group_id=group_id or 'split:'+str(members[0].id)
    renumber(members,group_id)
    # Preserve already processed children, never move them backwards.
    for o in members:
        if processed(o) and o.status!='cancelled':record_release(session,group_id,o,o.created_at)
    existing=[o for o in members if o not in created and o.status=='pending' and not processed(o)]
    released=session.execute(text('SELECT order_id FROM split_release_days WHERE group_id=:g AND day=:d'),{'g':group_id,'d':day(now)}).scalar()
    if existing:
        first=min(existing,key=lambda o:(o.created_at,o.id))
        record_release(session,group_id,first,first.created_at)
        released=first.id
    if not released:
        first=next((o for o in members if o.status!='cancelled' and not processed(o)),None)
        from address_daily_orders import predecessor_ready
        if first and predecessor_ready(session,first,now):
            record_release(session,group_id,first,now);released=first.id
    for o in members:
        if o.status!='cancelled' and not processed(o):o.status='pending' if o.id==released else 'split_pending'


def tick(database,now=None,*,approved_historical_release=None,release_guard=None):
    """Atomically admit the whole scheduler operation before deployment pause."""
    from deployment_drain import begin_activity,end_activity
    activity=begin_activity()
    if activity is None:return []
    try:
        return _tick_active(database,now,approved_historical_release=approved_historical_release,release_guard=release_guard)
    finally:
        end_activity(activity)


def _validate_newbot_daily_policy(settings):
    """Fail closed on stale config; never silently rewrite runtime settings.

    The clone contract is one admitted child/address/Beijing day. Parcel sizes
    remain owned by the existing catalog/parcel planner, not this scheduler.
    Original-account behavior remains unchanged.
    """
    from telegram_source_identity import account_cutover
    if account_cutover() is None:return
    orders=getattr(settings,'ADDRESS_DAILY_MAX_ORDERS',None)
    units=getattr(settings,'ADDRESS_DAILY_MAX_UNITS',None)
    if (type(orders) is not int or orders!=1 or
            type(units) is not int or units!=0):
        raise ValueError('NEWBOT_SPLIT_DAILY_POLICY_MISMATCH')


def _tick_active(database,now=None,*,approved_historical_release=None,release_guard=None):
    """The existing DB writer lock serializes every admitted scheduler."""
    now=now or datetime.utcnow();released=[]
    import config
    config.load_runtime_settings()
    _validate_newbot_daily_policy(config)
    from models import Order
    order_cap=int(getattr(config,'ADDRESS_DAILY_MAX_ORDERS',0) or 0)
    from address_daily_orders import prewarm
    normalization=prewarm(database,now) if order_cap else {}
    with database.SessionLocal() as session:
        session.info['address_daily_normalization']=normalization
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        if not ready(session):return []
        from address_daily_orders import ready as address_ready, used, record
        if order_cap and not address_ready(session):raise ValueError('同地址每日订单数Migration未完成')
        groups={o.split_group_id for o in session.query(Order).filter(Order.status=='split_pending').all() if o.split_group_id}
        for g in sorted(groups):
            members=session.query(Order).filter(Order.split_group_id==g).order_by(Order.split_index,Order.id).all()
            for o in members:
                if (o.status=='split_pending' and _recording_succeeded(o)
                        and (release_guard is None or release_guard(o,members))):o.status='confirmed'
            waiting=[o for o in members if o.status=='split_pending' and not processed(o)]
            if not waiting:continue
            if any(o.status=='pending' and not processed(o) for o in members):continue
            candidate=waiting[0]
            if not _eligible_release(session,candidate,members,now,approved_historical_release,release_guard):continue
            if order_cap:
                available=max(0,order_cap-used(session,waiting[0].recipient_address,now))
                # Spare slots never authorize a burst of future successors.
                if available:
                    candidate.status='pending';record_release(session,g,candidate,now)
                    record(session,candidate,now);released.append(candidate.id)
                continue
            if session.execute(text('SELECT 1 FROM split_release_days WHERE group_id=:g AND day=:d'),{'g':g,'d':day(now)}).scalar():continue
            # No catch-up burst: a missed week still releases just one today.
            candidate=waiting[0];candidate.status='pending';record_release(session,g,candidate,now);released.append(candidate.id)
        session.commit()
    return released


def message(order,members):
    s=summary(members)
    warehouses=list(dict.fromkeys(i.warehouse.name for i in order.items))
    goods='、'.join(f'{i.product.get_full_name()}×{i.quantity}' for i in order.items)
    # The dedicated group is an authorized operational destination; phone masked.
    phone=str(order.recipient_phone or '')
    if len(phone)>7:phone=phone[:3]+'*'*(len(phone)-7)+phone[-4:]
    return '\n'.join(['📦 分单订单','',f'#{order.order_number}',f'分单 {order.split_index}/{order.split_count}',
        '仓库：'+'、'.join(warehouses),f'收件人：{order.recipient_name} {phone}','商品：'+goods,'',s['progress'],f"剩余{s['remaining']}单"])


def _begin_immediate(session):
    """Avoid a SQLite read-then-write transaction upgrade race."""
    if session.get_bind().dialect.name == 'sqlite':
        session.execute(text('BEGIN IMMEDIATE'))


async def sync_messages(database,transport):
    """All callers share pause admission, activity tracking and a sender lock.

    Hold the activity through every HTTP await and durable outcome. The outer
    scope covers prompt preparation; each action is admitted separately so a
    pause during one request cannot start the next one. File locks are OS-owned
    and nonblocking, including across API workers and process death.
    """
    from deployment_drain import begin_activity,end_activity,path
    activity=begin_activity()
    if activity is None:return
    lock=None
    try:
        lock=os.open(str(path())+'.split-notifications.lock',
                     os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        await _sync_messages_active(database,transport)
    finally:
        if lock is not None:os.close(lock)
        end_activity(activity)


def _notice_outcome(database,row,claim,status,*,message_id=None,sent=False,error=None):
    """Finalize only our durable claim; never change order/stock/ledger facts."""
    with database.SessionLocal() as session:
        _begin_immediate(session)
        changed=session.execute(text('''UPDATE split_waiting_messages
            SET status=:status,last_error=:error,updated_at=:now,
                message_id=CASE WHEN :message_id IS NULL THEN message_id ELSE :message_id END,
                sent_payload=CASE WHEN :sent THEN :payload ELSE sent_payload END
            WHERE order_id=:id AND status=:claim'''),
            dict(status=status,error=error,now=datetime.utcnow().isoformat(),
                 message_id=message_id,sent=sent,payload=row['payload'],id=row['order_id'],claim=claim)).rowcount
        if changed!=1:raise RuntimeError('SPLIT_NOTICE_CLAIM_CHANGED')
        session.commit()


async def _sync_messages_active(database,transport):
    """Order state wins. Unknown transport outcomes are never auto-replayed."""
    from models import Order
    from deployment_drain import begin_activity,end_activity
    from success_receipt_audit import append as audit
    import config
    target=str(config.SPLIT_NOTIFICATION_CHAT_ID)
    if not target:return
    from telegram_source_identity import account_cutover
    boundary=account_cutover()
    after=boundary.replace(tzinfo=None) if boundary else None
    with database.SessionLocal() as session:
        # Reserve the write slot before reading rows that this same short
        # transaction may upsert.  In WAL mode, starting as a reader and then
        # upgrading after another thread commits can fail immediately with
        # SQLITE_BUSY despite busy_timeout.  Commit before any HTTP await.
        _begin_immediate(session)
        if not ready(session):return
        waiting=[o for o in session.query(Order).filter(Order.status=='split_pending').all()
                 if not processed(o) and (after is None or o.created_at is not None and o.created_at>=after)]
        for o in waiting:
            members=session.query(Order).filter(Order.split_group_id==o.split_group_id).all()
            payload=message(o,members)
            session.execute(text('INSERT OR IGNORE INTO split_waiting_messages(order_id,chat_id,payload,updated_at) VALUES(:id,:chat,:p,:now)'),
                            {'id':o.id,'chat':target,'p':payload,'now':datetime.utcnow().isoformat()})
            session.execute(text('UPDATE split_waiting_messages SET payload=:p WHERE order_id=:id'),{'id':o.id,'p':payload})
        session.commit()
        rows=[dict(r) for r in session.execute(text('SELECT * FROM split_waiting_messages')).mappings()]
    for row in rows:
        activity=begin_activity()
        if activity is None:break
        try:
            with database.SessionLocal() as session:
                _begin_immediate(session)
                o=session.get(Order,row['order_id']);wait=bool(o and o.status=='split_pending' and not processed(o))
                if after is not None and (o is None or o.created_at is None or o.created_at<after):
                    continue  # Old-bot prompt IDs are not editable/deletable by this account.
                if not wait:
                    action='delete' if row['message_id'] and row['status']=='sent' else None
                elif row['status']=='pending':action='send'
                elif row['status']=='sent' and row['payload']!=row['sent_payload']:action='edit'
                else:action=None
                if not action:continue
                claim={'send':'sending','edit':'editing','delete':'deleting'}[action]
                changed=session.execute(text('''UPDATE split_waiting_messages
                    SET status=:claim WHERE order_id=:id AND status=:old'''),
                    dict(claim=claim,id=row['order_id'],old=row['status'])).rowcount
                if not changed:continue
                session.commit()
            evidence=dict(attempt_id=uuid.uuid4().hex,delivery_path='SPLIT_NOTIFICATION',
                          order_id=int(row['order_id']),action=action)
            try:
                # Content-free shared audit is also checked by idle_barrier.
                # A crash leaves SENDING, so OS lock release alone is not idle.
                audit(dict(evidence,status='SENDING'))
            except BaseException:
                # No transport was invoked: retrying this unattempted action
                # later is safe, but never send without durable audit first.
                _notice_outcome(database,row,claim,row['status'],error='split_notice_audit_unavailable')
                raise
            try:
                result=await transport(action,row['chat_id'],row.get('message_id'),row['payload'])
                message_id=int(result) if action=='send' else int(row['message_id'])
                if message_id<=0:raise ValueError('SPLIT_NOTICE_INVALID_ACK')
            except BaseException as error:
                permanent_delete=action=='delete' and any(code in str(error).lower() for code in (
                    "message can't be deleted",'message cannot be deleted',
                    'message to delete not found','message_id_invalid'))
                state='inactive' if permanent_delete else 'unknown' if action=='send' else 'unknown_'+action
                try:
                    _notice_outcome(database,row,claim,state,
                        error='telegram_delete_unavailable' if permanent_delete else 'telegram_'+action+'_unknown')
                finally:
                    audit(dict(evidence,status='NOT_SENT' if permanent_delete else 'UNKNOWN'))
                if not isinstance(error,Exception):raise
                continue
            try:
                _notice_outcome(database,row,claim,'deleted' if action=='delete' else 'sent',
                                message_id=message_id,sent=action!='delete')
            except BaseException:
                # Telegram acknowledged, but local finalization is uncertain.
                # Keep claim and a blocking audit, never issue the request again.
                audit(dict(evidence,status='UNKNOWN',message_id=message_id))
                raise
            audit(dict(evidence,status='SENT',message_id=message_id))
        finally:
            end_activity(activity)
