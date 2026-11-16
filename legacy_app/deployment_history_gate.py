"""Exact, immutable historical audit exceptions; never changes or sends rows."""
import hashlib,json,subprocess,time
import re
from pathlib import Path
from datetime import datetime,timezone
from sqlalchemy import text,inspect

TABLES=('telegram_execution_receipts','telegram_notification_events')
LEGACY_POLICY_SHA256='7b5629915112c4881da0c1d2cc43a0969b35f31596afb794768d95d6eec20bb7'
EXTENSION_KEYS_SHA256='5b950de8f76c3fadfae5f2e2a927ec62f6ac33938f96810407d1931b13262851'
def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def links(session,chat,message):
    args=dict(chat=str(chat),message=int(message))
    jobs=[dict(r) for r in session.execute(text('''SELECT namespace,message_id,state,started_at,finished_at,lease_until,lease_token,attempts,reason
        FROM telegram_order_jobs WHERE chat_id=:chat AND message_id=:message ORDER BY namespace'''),args).mappings()]
    commits=[dict(r) for r in session.execute(text('''SELECT namespace,order_id FROM telegram_execution_commits
        WHERE chat_id=:chat AND message_id=:message ORDER BY namespace,order_id'''),args).mappings()]
    orders=[dict(r) for r in session.execute(text('''SELECT id,order_number,business_order_number FROM orders
        WHERE source_chat_id=:chat AND source_message_id=:message ORDER BY id'''),args).mappings()]
    return dict(jobs=jobs,commits=commits,orders=orders)

def service_boundaries():
    result={}
    for unit in ('tg-order-bot','tg-order-api'):
        p=subprocess.run(['systemctl','show',unit,'-p','MainPID','-p','ActiveState','-p','ExecMainStartTimestamp'],capture_output=True,text=True,timeout=4,check=True)
        v=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
        if v.get('MainPID')=='0' and v.get('ActiveState') in ('inactive','failed'):result[unit]=time.time();continue
        if not v.get('MainPID','').isdigit() or int(v['MainPID'])<=0:raise RuntimeError('DRAIN_HISTORY_PROCESS_UNVERIFIED')
        stamp=v.get('ExecMainStartTimestamp','')
        if not stamp.endswith(' UTC'):raise RuntimeError('DRAIN_HISTORY_PROCESS_UNVERIFIED')
        result[unit]=datetime.strptime(stamp,'%a %Y-%m-%d %H:%M:%S UTC').replace(tzinfo=timezone.utc).timestamp()
    return result

def typed_snapshot(session,row,rule):
    """Fresh authoritative links; never trusts a policy's claimed task state.

    Used only under idle_barrier's OS activity/admission and SQLite writer
    locks. Historical delivery remains unknown even when this check succeeds.
    """
    if row['status']!='unknown' or row['attempts']!=1 or row.get('delivered_message_id') is not None:
        raise RuntimeError('DRAIN_HISTORY_TYPED_ROW_UNSAFE')
    kind=rule.get('proof_type')
    if row.get('edit_message_id') is not None:
        raise RuntimeError('DRAIN_HISTORY_TYPED_ROW_UNSAFE')
    if row.get('source_message_id') is not None:
        if rule['table']!=TABLES[0] or str(row['chat_id'])!=str(rule['chat_id']) or row['source_message_id']!=rule['source_message_id']:
            raise RuntimeError('DRAIN_HISTORY_TYPED_IDENTITY')
        result=links(session,row['chat_id'],row['source_message_id'])
        jobs=result['jobs']
        expected={'completed_receipt':'COMPLETED','timed_out_receipt':'TIMED_OUT'}.get(kind)
        if not expected or not jobs or any(j['state']!=expected or j['lease_until'] is not None or j['lease_token'] is not None for j in jobs):
            raise RuntimeError('DRAIN_HISTORY_ACTIVE_OR_UNVERIFIED_TASK')
        if expected=='COMPLETED':
            if not result['orders'] or {o['id'] for o in result['orders']}!={c['order_id'] for c in result['commits']}:
                raise RuntimeError('DRAIN_HISTORY_COMMIT_UNVERIFIED')
        else:
            if result['orders'] or result['commits']:
                raise RuntimeError('DRAIN_HISTORY_TIMEOUT_HAS_COMMIT')
            fences=[dict(r) for r in session.execute(text('SELECT namespace,chat_id,message_id FROM telegram_source_fences WHERE chat_id=:chat AND message_id=:message ORDER BY namespace'),dict(chat=str(row['chat_id']),message=row['source_message_id'])).mappings()]
            if any(not any(f['namespace'] in (j['namespace'],'legacy:unknown') for f in fences) for j in jobs):
                raise RuntimeError('DRAIN_HISTORY_SOURCE_FENCE_UNVERIFIED')
            result['fences']=fences
        return result
    if rule['table']!=TABLES[1]:raise RuntimeError('DRAIN_HISTORY_TYPED_IDENTITY')
    key=row['event_key']
    if kind=='cancel_notice':
        match=re.fullmatch(r'cancel:([0-9a-f]{32})',key)
        if not match:raise RuntimeError('DRAIN_HISTORY_TYPED_IDENTITY')
        facts=[dict(r) for r in session.execute(text('SELECT order_id,cancelled_at FROM order_cancellation_facts WHERE event_id=:id'),dict(id=match[1])).mappings()]
        if len(facts)!=1:raise RuntimeError('DRAIN_HISTORY_CANCEL_UNVERIFIED')
        order=session.execute(text('SELECT id,status,source_chat_id,source_message_id FROM orders WHERE id=:id'),dict(id=facts[0]['order_id'])).mappings().one_or_none()
        payload=json.loads(row['payload'])
        if not order or order['status']!='cancelled' or str(order['source_chat_id'])!=str(row['chat_id']) or payload.get('reply_to_message_id')!=order['source_message_id']:
            raise RuntimeError('DRAIN_HISTORY_CANCEL_UNVERIFIED')
        result=links(session,order['source_chat_id'],order['source_message_id'])
        if not result['jobs'] or any(j['state']!='COMPLETED' or j['lease_until'] is not None or j['lease_token'] is not None for j in result['jobs']):
            raise RuntimeError('DRAIN_HISTORY_ACTIVE_OR_UNVERIFIED_TASK')
        return dict(facts=facts,order=dict(order),links=result)
    if kind=='payment_notice':
        match=re.fullmatch(r'payment-v2:(\d+):(\d+):([0-9a-f]{32})',key)
        if not match:raise RuntimeError('DRAIN_HISTORY_TYPED_IDENTITY')
        payment=session.execute(text('SELECT id,user_id,created_at FROM payments WHERE id=:id'),dict(id=int(match[1]))).mappings().one_or_none()
        if not payment or str(payment['user_id'])!=match[2] or str(row['user_id'])!=match[2]:
            raise RuntimeError('DRAIN_HISTORY_PAYMENT_UNVERIFIED')
        stamp=datetime.fromisoformat(str(payment['created_at']))
        stamp=stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        expected=hashlib.sha256(f"{payment['id']}:{payment['user_id']}:{stamp.isoformat()}".encode()).hexdigest()[:32]
        if expected!=match[3]:raise RuntimeError('DRAIN_HISTORY_PAYMENT_UNVERIFIED')
        return dict(payment=dict(payment))
    raise RuntimeError('DRAIN_HISTORY_PROOF_TYPE_INVALID')

def validate(session,policy=None):
    """Called inside drain's writer barrier after OS activity/lease checks."""
    current={}
    for table in TABLES:
        if inspect(session.connection()).has_table(table):
            for row in session.execute(text('SELECT * FROM '+table+" WHERE status IN ('sending','unknown')")).mappings():
                current[(table,row['event_key'])]=dict(row)
    if not policy:
        if current:raise RuntimeError('DRAIN_AMBIGUOUS_OUTBOX_SENDING_OR_UNKNOWN')
        return []
    version=policy.get('version')
    if version not in (1,2) or len(policy.get('records',[]))!=(7 if version==1 else 18):raise RuntimeError('DRAIN_HISTORY_POLICY_INVALID')
    legacy_keys=set()
    if version==2:
        legacy_path=Path(__file__).with_name('deployment_history_exceptions.json')
        raw=legacy_path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=LEGACY_POLICY_SHA256:raise RuntimeError('DRAIN_HISTORY_LEGACY_POLICY_CHANGED')
        legacy=json.loads(raw)['records']
        legacy_keys={(r['table'],r['event_key']) for r in legacy}
        supplied={(r['table'],r['event_key']):r for r in policy['records']}
        if any(supplied.get((r['table'],r['event_key']))!=r for r in legacy):raise RuntimeError('DRAIN_HISTORY_LEGACY_POLICY_CHANGED')
        extension=sorted([list(k) for k in supplied if k not in legacy_keys])
        if digest(extension)!=EXTENSION_KEYS_SHA256:raise RuntimeError('DRAIN_HISTORY_EXTENSION_SET_CHANGED')
    approved={}
    for rule in policy['records']:
        key=(rule['table'],rule['event_key'])
        if rule['table'] not in TABLES or key in approved or not rule.get('reason') or not rule.get('audit_sha256'):raise RuntimeError('DRAIN_HISTORY_POLICY_INVALID')
        approved[key]=rule
    if set(current)!=set(approved):raise RuntimeError('DRAIN_HISTORY_SET_CHANGED')
    boundaries=None
    for key,row in current.items():
        rule=approved[key]
        if digest(row)!=rule['row_sha256']:raise RuntimeError('DRAIN_HISTORY_RECORD_CHANGED')
        if version==2 and key not in legacy_keys:
            if not re.fullmatch('[0-9a-f]{64}',rule.get('proof_sha256','')) or digest(typed_snapshot(session,row,rule))!=rule['proof_sha256']:
                raise RuntimeError('DRAIN_HISTORY_TYPED_LINKS_CHANGED')
            continue
        snapshot=links(session,rule['chat_id'],rule['source_message_id'])
        if not snapshot['jobs'] or any(j['state']!='COMPLETED' or j['lease_until'] is not None or j['lease_token'] is not None for j in snapshot['jobs']):raise RuntimeError('DRAIN_HISTORY_ACTIVE_OR_UNVERIFIED_TASK')
        if digest(snapshot)!=rule['links_sha256']:raise RuntimeError('DRAIN_HISTORY_LINKS_CHANGED')
        if row['status']=='sending':
            if boundaries is None:boundaries=service_boundaries()
            proof=rule.get('observed_sending_before_epoch')
            if not isinstance(proof,(float,int)) or not rule.get('prior_backup_sha256') or any(t<=proof for t in boundaries.values()):raise RuntimeError('DRAIN_HISTORY_PROCESS_UNVERIFIED')
    return [dict(table=key[0],event_key=key[1],status=current[key]['status'],reason=approved[key]['reason']) for key in sorted(approved)]
