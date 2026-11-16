"""Independent exact18274 CONNECT timeout hold. UNKNOWN remains unproven.

Reuse the existing direct/recovery/transport guards; never writes or sends.
Prior128 reviews and the separate recovery/cancellation scopes stay untouched.
"""
import hashlib,json,logging,os,fcntl
from pathlib import Path
from timeout_receipt_hold import digest,job_metadata

ORDER_ID=18274
APPROVED_ATTEMPT='210d30879bad225a87a0d81a96dd6721d3130ac5d85fcc157da8546391c763f8'
PIN={'_scope': 'CONNECT18274',
 'attempt_ref': '210d30879bad225a87a0d81a96dd6721d3130ac5d85fcc157da8546391c763f8',
 'job_metadata_sha256': '5926545652959ca0ed4fcc3d405e6d81a2815fb8642c66e3e59cf1e2a511e2a5',
 'number_sha256': 'a5b767ab67e2c9a7de5f30d5394f6fcbac7b71a9d878e3014f4ac3e30e45dfca',
 'order_id': 18274,
 'original_sending_sha256': 'c4fc2cb0547492666663a2b1fa20d474e515cc2e7e2daccac8456a47234bb245',
 'public_number_sha256': 'ab8f8bdf25e8cd789ff05b7f1aef5cdd7bb6936cca5372c9c119be331fb6e9d5',
 'source_identity_sha256': '2f4cf66ae2d6670c553944e3c8221beba12baccc940384d5720083b48cb8c3ed',
 'source_pair_sha256': '5547361098fdbf5f4039eef8ecafe8c948d781eaa8b8605aa24c82d75971cd30',
 'terminal_links_sha256': 'eaa50243b7e192551927373a10cecdd3896bc2620332a3190f67d1fd59685632',
 'unknown_row_sha256': '2ae616a4c1429736ac1cc8ff417ca428dd823a0d41163b4bda0f3c41f5e0c965'}


def scope():
    if PIN.get('order_id')!=ORDER_ID or PIN.get('attempt_ref')!=APPROVED_ATTEMPT or PIN.get('_scope')!='CONNECT18274':
        raise ValueError('CONNECT18274_EXACT_SCOPE_REQUIRED')
    return PIN


def hashed(value):return hashlib.sha256(str(value).encode()).hexdigest()


def deny(record,channel):
    from six_unknown_receipt_hold import ReceiptReplayHeld
    reason='CONNECT18274_ORIGINAL_ORDER_RECEIPT_HELD'
    logging.getLogger(__name__).warning('%s source=%s channel=%s attempt_created=false',reason,scope()['source_identity_sha256'][:16],channel)
    raise ReceiptReplayHeld(reason)


def guard_direct(facts,order_id=None):
    # ReceiptSender is the success-receipt channel, not a blanket order ban.
    from execution_safety import CURRENT
    job=(CURRENT.get() or {}).get('job') or {};pin=scope()
    source=hashed(f"{job['namespace']}:{job['chat_id']}:{job['message_id']}") if all(k in job for k in ('namespace','chat_id','message_id')) else None
    numbers={hashed(n) for n in facts.get('order_numbers',[])}
    if source==pin['source_identity_sha256'] or str(order_id)==str(pin['order_id']) or numbers & {pin['number_sha256'],pin.get('public_number_sha256')}:
        deny(pin,'direct_order_success')


def held_recovery(event_key,chat,message):
    pin=scope()
    if hashed(f'{chat}:{message}')==pin['source_pair_sha256'] or str(event_key).startswith('final:'+pin['source_identity_sha256']+':'):
        return pin
    return None


def guard_transport(data,policy):
    if policy.get('order_receipt') is not True:return
    reply=data.get('reply_parameters') or {}
    if isinstance(reply,str):
        try:reply=json.loads(reply)
        except ValueError:reply={}
    msg=reply.get('message_id') if isinstance(reply,dict) else getattr(reply,'message_id',None)
    pin=held_recovery('',data.get('chat_id'),msg or data.get('reply_to_message_id'))
    if pin:deny(pin,'order_receipt_transport')
    from execution_safety import CURRENT
    job=(CURRENT.get() or {}).get('job') or {};pin=scope()
    if all(k in job for k in ('namespace','chat_id','message_id')) and hashed(f"{job['namespace']}:{job['chat_id']}:{job['message_id']}")==pin['source_identity_sha256']:
        deny(pin,'order_receipt_context')


def partition_verified(unresolved,connection):
    """Exact non-replay evidence, not evidence that the original was delivered."""
    from frozen_direct_receipts import terminal_links
    pin=scope();selected=[r for r in unresolved if hashed(r['attempt_id'])==pin['attempt_ref']]
    if len(selected)!=1:raise ValueError('CONNECT18274_EXACT_SET_CHANGED')
    row=selected[0]
    if row['status']!='UNKNOWN' or digest(row)!=pin['unknown_row_sha256']:
        raise ValueError('CONNECT18274_ORIGINAL_ROW_CHANGED')
    if digest(terminal_links(connection,row))!=pin['terminal_links_sha256']:
        raise ValueError('CONNECT18274_TERMINAL_LINKS_CHANGED')
    order=connection.execute('SELECT source_chat_id,source_message_id,order_number,business_order_number FROM orders WHERE id=?',(ORDER_ID,)).fetchone()
    if (order is None or hashed(order[2])!=pin['number_sha256'] or hashed(f'{order[0]}:{order[1]}')!=pin['source_pair_sha256']
            or (hashed(order[3]) if order[3] else None)!=pin['public_number_sha256']):
        raise ValueError('CONNECT18274_SOURCE_CHANGED')
    links=connection.execute('SELECT namespace,chat_id,message_id FROM telegram_execution_commits WHERE order_id=?',(ORDER_ID,)).fetchall()
    if len(links)!=1 or digest(job_metadata(connection,tuple(links[0])))!=pin['job_metadata_sha256']:
        raise ValueError('CONNECT18274_JOB_CHANGED')
    if connection.execute('SELECT 1 FROM telegram_execution_receipts WHERE chat_id=? AND source_message_id=? LIMIT 1',(order[0],order[1])).fetchone():
        raise ValueError('CONNECT18274_RECOVERY_ROW_PRESENT')
    path=Path(os.getenv('RAINBOW_RECEIPT_AUDIT_PATH','success_receipt_audit.jsonl'))
    if path.stat().st_size>32*1024*1024:raise ValueError('CONNECT18274_AUDIT_TOO_LARGE')
    chain=[]
    with path.open() as f:
        fcntl.flock(f,fcntl.LOCK_SH|fcntl.LOCK_NB)
        for line in f:
            value=json.loads(line)
            if hashed(value['attempt_id'])==pin['attempt_ref']:chain.append(value)
            elif (value.get('task_id')==pin['source_identity_sha256'][:16] or {hashed(n) for n in value.get('order_numbers',[])} & {pin['number_sha256'],pin.get('public_number_sha256')}):
                raise ValueError('CONNECT18274_NEW_ATTEMPT_FOR_SOURCE')
    if (len(chain)!=2 or [r['status'] for r in chain]!=['SENDING','UNKNOWN'] or digest(chain[0])!=pin['original_sending_sha256'] or digest(chain[1])!=pin['unknown_row_sha256']):
        raise ValueError('CONNECT18274_HISTORY_CHANGED')
    return [r for r in unresolved if r['attempt_id']!=row['attempt_id']],[row['attempt_id']]
