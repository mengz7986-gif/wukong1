"""Exact six historical order-success holds; no status updates or send retries.

Approved 2026-11-03: historical delivery remains UNKNOWN. Other notifications
use their own notification-event identity, never this order-success channel.
"""
import hashlib,json,os,fcntl,logging,re
from pathlib import Path

APPROVED_ORDER_IDS=(15881,16058,16247,16275,16486,16511)
SCOPE=({'order_id': 15881,
  'source_identity_sha256': '747d87d9f7dc8fb29e0895d64894cfdd6f0a150b760b9fb51b0ae30474fc9fa8',
  'attempt_ref': '1d08542da379b2881ef73af4c11c04f1df61602fa5949ed16cb8f62fbdd70763',
  'original_sending_sha256': '64058bebb67ef0c6e8865a33680f2d0ed189fedf4926ba380f490f74b9d1097a',
  'unknown_row_sha256': '56648a3a63cef898b759761f7fbc301670762ca471fbd37e8adced40135b7f06',
  'terminal_links_sha256': '1fbfa342803172d7b017b547e1dff487aaf06c7bc3a57c20f52287a3dc505064',
  'job_fingerprint': 'c4d18ec2d3a23d4b6e08d4e7d0db1682474538c95b2f5b4354c858d4aafb801e',
  'job_row_sha256': 'd1df0c838d0d4bcf6f4bf92e919b589dda68661b5710daa1fd42a9c1362f94f0',
  'number_sha256': '4c082b39fdf8a9d28fb6aef1c409ccd65490acb96b39ff9c484621796a6c7fcd',
  'source_pair_sha256': '6cf8e87c963a04415d8cdf62ad6f4ac673831a8656e8abafd7ef732a8fd6ddb6'},
 {'order_id': 16058,
  'source_identity_sha256': '10c95b2f6ccf149884cf66eef99ca6c72290e95f191d2c79f301f5ce157e3652',
  'attempt_ref': '9961a910c31649a9c1314282dcbb8b1c5f1a68f6dcf12703c36d97cd9a40055a',
  'original_sending_sha256': '813c60dfa44480534b07ccad6e1bbaebd5e4de8b8ede2d047aad6d01ccd01683',
  'unknown_row_sha256': '39af057f25b3254aa0bfba30852dc3cacd24c34fddfde8c64a04c96116271b95',
  'terminal_links_sha256': 'aff47396df2b4f2e035607a665a5de5f72c7f288a0eefbbe76918c525299b72d',
  'job_fingerprint': '7d646cc2467d6756da8b783eae6b7d49c93b574c1eccb0afe92cf29be36629aa',
  'job_row_sha256': 'ca29c6c2be84986319f4f1c44b8146a2a24814588851e0a1221fc4a41aa58cb2',
  'number_sha256': '863100228f5c7cf0ed8c1dea115d16e1b748fb8667e35cfd63e9db89854099f2',
  'source_pair_sha256': 'aa8a87e77a858350801d5878418d6157c158d23312d9b5d3cd513de8448e5893'},
 {'order_id': 16247,
  'source_identity_sha256': '510412fee03718b2b42ef585306f3d332d97782d5e858fd5d4b9b3114b65c13f',
  'attempt_ref': 'e7b0d2c5665e8112df4ce5da8fae40e5dd17fd95f7050d44e605ff2c7fb5c894',
  'original_sending_sha256': '977279315048b44caa99130038ef9cf69925bbd87a80f1cc91b7a4593a127fc7',
  'unknown_row_sha256': '100ca5a57e9f20060f3ea208dba7dc9c80de45dc43bf138a624ad51d24802eae',
  'terminal_links_sha256': '34f755b6d2a88fd5001ffd4d3c51d23851808f5c032b93275b82c04346979c10',
  'job_fingerprint': '82b8abb1db2dfe13ab8cfd6989a16276be8501e3ec86a396b169e2f6fcba9e4f',
  'job_row_sha256': '8a07c613504274432f4c82b714af832376e5f73a29e14951b4f92b69cf287b46',
  'number_sha256': '46405ef688ac572f3e06cd4d7ce36dcda6a9469f2aa5cd399cbcc802997cabdc',
  'source_pair_sha256': '756f7ae467440d1ecc882a947a5334a131636aad7bfa0a08cadbdd4db3f1a5b7'},
 {'order_id': 16275,
  'source_identity_sha256': '3c2d1c4b2861a4d0409e852e829536d40ebcd6ae9888ea7e313b0c24c90b43bf',
  'attempt_ref': 'c406ae0ca7c0e9aa266fadecf67d70bf7f094e744afa627b1e211246d35dd8fe',
  'original_sending_sha256': 'dda08cfe2d73eb738d7cae78574f9da4eb0f39a5be468040b1292ce7a2aceb82',
  'unknown_row_sha256': 'c28b1501154e8b2f547503b1e4a6e460b7f8f0c36c2a37aaef2772654bda9273',
  'terminal_links_sha256': '5aa954339e50b56adfeffab141f747f3b7992a1baa11f560f1f855f435629005',
  'job_fingerprint': 'daed1fafedb033f736297af3b3b79b0cb9b7d80a5b34d30a46c2df37fc8cf44e',
  'job_row_sha256': 'cfd9fb8725e1e283077b9ad7a84ee89ef4d079908e1e2b9bb3126e4698671b86',
  'number_sha256': '4f8fd9b246f22db3e07725253e326880e467b34e989bdc4da575549aef542d4b',
  'source_pair_sha256': '8f4ac809d3ada1f395419cceaf8b00e799fc38cb74f5d54b9d2aed728185a80f'},
 {'order_id': 16486,
  'source_identity_sha256': '45508b241c545c35f2ecdcf28d06cdc5108c23a12d05e2ffbc6940bf0f98c07f',
  'attempt_ref': '62976af179050a2d6569c13fb3b36fa5e017bae8cad0503339e638b64e40afc5',
  'original_sending_sha256': 'd924d141b20cbc0453d8629a6b4cbdd87bd85ac97bfa6eb74b2e379ec6a6b662',
  'unknown_row_sha256': '56f6213ccb1b220b1181b1e7af23fa27df92b8c5da501c47dfc92075f192de2d',
  'terminal_links_sha256': '062bd3ed33eae76d37ae3ef55bcc6ad788676d48d9c06762303a4447e0e72fe1',
  'job_fingerprint': '2fb624b67905470511f7371a4369b20c52114289d5b094b8c7bef36d4d8aa820',
  'job_row_sha256': '35a6f0fd5c00a4e42381cb255af22026ac8223f444bc0cd08ca86076fbed4fa5',
  'number_sha256': 'b94351395561e27420b2ef60ef148213b983e680b8c8c6a2969d863f52b41187',
  'source_pair_sha256': '347f58aac498a1fdc160e1dcf181410366970dbb37d5f919650b0d6a44fb7391'},
 {'order_id': 16511,
  'source_identity_sha256': 'caa0fa1b67a31f267d9fa1fc893628b13a8044283d94c5127efb6b3eca21acc5',
  'attempt_ref': '6a8e329f24bbdf6a89054b64808a6f54b55c737c4f9ffcdfc63eaf89269cd0d5',
  'original_sending_sha256': 'd757cac951d8c64267d99f945a96d90d60b7938c000861f9c16275b388cc69af',
  'unknown_row_sha256': '4eac030b4434232170d133f3591574bf59465bd8fd83ce726a5290ceb661f0c4',
  'terminal_links_sha256': 'be95fe18991f352352ec76d413f5f5b08b8e1b02e535f5bfef504b98556487cf',
  'job_fingerprint': '0db7206f9f957a98f0d9c0f56c9ce1cafbc6a574ac0cf2907b0a2899e9ac8649',
  'job_row_sha256': 'a5a78f78df9aa9e16c51d114c5afc55ec8bc720d103ccc9ccd09a0d28b126d7e',
  'number_sha256': '646d6b917b457411cc170da8c60e5d0a04ecb584a10f6091a14d63e6106787c1',
  'source_pair_sha256': 'bc11f6f6210d7ea709034d91a167f1bdca7371adaf122d48393d668e5ec99880'})

class ReceiptReplayHeld(RuntimeError):pass

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def scope():
    if len(SCOPE)!=6 or {r['order_id'] for r in SCOPE}!=set(APPROVED_ORDER_IDS):
        raise ValueError('SIX_HOLD_SCOPE_INVALID')
    for key in ('attempt_ref','source_identity_sha256','source_pair_sha256','number_sha256'):
        if len({r[key] for r in SCOPE})!=6 or any(not re.fullmatch('[a-f0-9]{64}',r[key]) for r in SCOPE):
            raise ValueError('SIX_HOLD_SCOPE_INVALID')
    return SCOPE

def deny(record,channel):
    if record.get('_scope')=='CONNECT18274':
        from connect_receipt_hold import deny as deny_connect
        return deny_connect(record,channel)
    if record.get('_scope')=='RECOVERY7':
        from recovery_timeout_hold import deny as deny_recovery
        return deny_recovery(record,channel)
    if record.get('_scope')=='TIMEOUT36':
        from timeout_receipt_hold import deny as deny_timeout
        return deny_timeout(record,channel)
    reason='SIX_UNKNOWN_ORIGINAL_RECEIPT_HELD'
    logging.getLogger(__name__).warning('%s source=%s channel=%s attempt_created=false',reason,record['source_identity_sha256'][:16],channel)
    raise ReceiptReplayHeld(reason)

def guard_direct(facts,order_id=None):
    # ReceiptSender is only the order-success confirmation channel. Separate
    # account/cancellation/fulfilment notification dispatchers stay unchanged.
    numbers={hashlib.sha256(str(n).encode()).hexdigest() for n in facts.get('order_numbers',[])}
    from execution_safety import CURRENT
    job=(CURRENT.get() or {}).get('job') or {}
    source=None
    if all(k in job for k in ('namespace','chat_id','message_id')):
        source=hashlib.sha256(f"{job['namespace']}:{job['chat_id']}:{job['message_id']}".encode()).hexdigest()
    if isinstance(order_id,str) and order_id.strip().isdigit():order_id=int(order_id.strip())
    if not numbers and source is None and type(order_id) is not int:
        logging.getLogger(__name__).warning('ORDER_SUCCESS_RECEIPT_IDENTITY_REQUIRED attempt_created=false')
        raise ReceiptReplayHeld('ORDER_SUCCESS_RECEIPT_IDENTITY_REQUIRED')
    for r in scope():
        if source==r['source_identity_sha256'] or r['number_sha256'] in numbers or str(order_id)==str(r['order_id']):
            deny(r,'direct_order_success')
    from timeout_receipt_hold import guard_direct as guard_timeout
    guard_timeout(facts,order_id)
    from recovery_timeout_hold import guard_direct as guard_recovery
    guard_recovery(facts,order_id)
    from connect_receipt_hold import guard_direct as guard_connect
    guard_connect(facts,order_id)

def held_recovery(event_key,chat,message):
    pair=hashlib.sha256(f'{chat}:{message}'.encode()).hexdigest()
    for r in scope():
        if str(event_key).startswith('final:'+r['source_identity_sha256']+':') or pair==r['source_pair_sha256']:
            return r
    from timeout_receipt_hold import held_recovery as held_timeout
    held=held_timeout(event_key,chat,message)
    if held:return held
    from recovery_timeout_hold import held_recovery as held_seven
    held=held_seven(event_key,chat,message)
    if held:return held
    from connect_receipt_hold import held_recovery as held_connect
    return held_connect(event_key,chat,message)

def guard_transport(data,policy):
    if policy.get('order_receipt') is not True:return
    reply=data.get('reply_parameters') or {}
    if isinstance(reply,str):
        try:reply=json.loads(reply)
        except ValueError:reply={}
    message=reply.get('message_id') if isinstance(reply,dict) else getattr(reply,'message_id',None)
    message=message or data.get('reply_to_message_id')
    r=held_recovery('',data.get('chat_id'),message)
    if r:deny(r,'order_receipt_transport')
    from execution_safety import CURRENT
    job=(CURRENT.get() or {}).get('job') or {}
    if all(k in job for k in ('namespace','chat_id','message_id')):
        source=hashlib.sha256(f"{job['namespace']}:{job['chat_id']}:{job['message_id']}".encode()).hexdigest()
        for r in scope():
            if r['source_identity_sha256']==source:deny(r,'order_receipt_transport')
    from timeout_receipt_hold import guard_transport as guard_timeout
    guard_timeout(data,policy)
    from connect_receipt_hold import guard_transport as guard_connect
    guard_connect(data,policy)

def partition_verified(unresolved,connection):
    """Read-only companion approval, never proof of original delivery ACK."""
    from frozen_direct_receipts import terminal_links
    pins={r['attempt_ref']:r for r in scope()}
    selected={hashlib.sha256(x['attempt_id'].encode()).hexdigest():x for x in unresolved if hashlib.sha256(x['attempt_id'].encode()).hexdigest() in pins}
    if set(selected)!=set(pins):raise ValueError('SIX_HOLD_EXACT_SET_CHANGED')
    for key,row in selected.items():
        pin=pins[key]
        if row['status']!='UNKNOWN' or digest(row)!=pin['unknown_row_sha256']:
            raise ValueError('SIX_HOLD_ORIGINAL_ROW_CHANGED')
        if digest(terminal_links(connection,row))!=pin['terminal_links_sha256']:
            raise ValueError('SIX_HOLD_TERMINAL_LINKS_CHANGED')
        order=connection.execute('SELECT source_chat_id,source_message_id,order_number FROM orders WHERE id=?',(pin['order_id'],)).fetchone()
        if hashlib.sha256(order[2].encode()).hexdigest()!=pin['number_sha256'] or hashlib.sha256(f'{order[0]}:{order[1]}'.encode()).hexdigest()!=pin['source_pair_sha256']:
            raise ValueError('SIX_HOLD_SOURCE_IDENTITY_CHANGED')
        job=connection.execute('SELECT * FROM telegram_order_jobs WHERE chat_id=? AND message_id=?',(order[0],order[1])).fetchall()
        if len(job)!=1 or digest(dict(job[0]))!=pin['job_row_sha256']:
            raise ValueError('SIX_HOLD_JOB_CHANGED')
        source=hashlib.sha256(f"{job[0]['namespace']}:{order[0]}:{order[1]}".encode()).hexdigest()
        if source!=pin['source_identity_sha256']:raise ValueError('SIX_HOLD_SOURCE_IDENTITY_CHANGED')
        if connection.execute('SELECT 1 FROM telegram_execution_receipts WHERE chat_id=? AND source_message_id=? LIMIT 1',(order[0],order[1])).fetchone():
            raise ValueError('SIX_HOLD_RECOVERY_ROW_PRESENT')
    # Pin both the original intent and terminal row; do not accept a newly
    # fabricated UNKNOWN in place of the historical chain.
    path=Path(os.getenv('RAINBOW_RECEIPT_AUDIT_PATH','success_receipt_audit.jsonl'))
    if path.stat().st_size>32*1024*1024:raise ValueError('SIX_HOLD_AUDIT_TOO_LARGE')
    chains={k:[] for k in pins}
    with path.open() as stream:
        fcntl.flock(stream,fcntl.LOCK_SH|fcntl.LOCK_NB)
        for line in stream:
            row=json.loads(line);ref=hashlib.sha256(row['attempt_id'].encode()).hexdigest()
            if ref in pins:chains[ref].append(row)
            elif any(row.get('task_id')==p['source_identity_sha256'][:16] for p in pins.values()):
                raise ValueError('SIX_HOLD_NEW_ATTEMPT_FOR_SOURCE')
    for key,chain in chains.items():
        if len(chain)!=2 or [x['status'] for x in chain]!=['SENDING','UNKNOWN'] or digest(chain[0])!=pins[key]['original_sending_sha256'] or digest(chain[1])!=pins[key]['unknown_row_sha256']:
            raise ValueError('SIX_HOLD_HISTORY_CHANGED')
    accepted={row['attempt_id'] for row in selected.values()}
    return [x for x in unresolved if x['attempt_id'] not in accepted],list(accepted)
