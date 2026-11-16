"""Seven separately reviewed recovery timeouts: immutable history, never resend.

No order-success or delivery claim is implied. Original P0 policy stays pinned;
this companion validates only these seven rows before the original 29-row hash.
"""
import hashlib
import json
import logging
import re
from sqlalchemy import text

APPROVED_EVENTS = ('ab54701f22fc7b7dea02135606a5a26d231c9da158b97fdbb8dfb50e9b586fbb',
 '3885d86703afabda18446f01427392a651fc4625de9ca9ab542823d103f0d2e6',
 'fffa08fce579c9928c9f6c10feec67bcb2368a5962c23cd21b50aaa4867f5535',
 'c236c3d3c9f88a8103026a66c78bfa9e72d4f3335b5dd2bb81fba42d27fbee05',
 '38076f05a8bd109c608642afa86aa18b7a92827b1d952843946e8a3144e3b853',
 '9ac263e62c0dfcbee1794911a483d4d2d12bf29f1bd0d09ff5996802aec59af6',
 '45de41cf317b460964db35ddd94a72742dc1c21de5384151a48e97fb365d23cf')
SCOPE = ({'drafts_sha256': 'a3ba245f367d2e258cd08a20a9f2c9e5b7cd8bd2c60973aada4a0ceb1bb5537a',
  'edit_pair_sha256': None,
  'event_sha256': 'ab54701f22fc7b7dea02135606a5a26d231c9da158b97fdbb8dfb50e9b586fbb',
  'fences_sha256': '24a154e452e67778b8def0c6a71df99bc4aaebef99371c926788d491829096b3',
  'job_sha256': '980d51573b01106116d1a8797da2dc2f7b23da6c83328b90b568d7ccdd880121',
  'receipt_sha256': 'e025168aaf09da8c6ecbf789af1ecb725158014d5cc3694505c6237ec3abf30e',
  'source_identity_sha256': '1339378096ec0d4e8adbb5f5ed6147ea1bf5cd8beca29457128521311650b22b',
  'source_pair_sha256': '24891a73415dd1cfaae0469cb7efe26019a743ba982befaf135f13674d40e254'},
 {'drafts_sha256': 'ed8f5404f50b4391812a5750a13d102cd4795d01bffa2923b78a9b31e1c63886',
  'edit_pair_sha256': None,
  'event_sha256': '3885d86703afabda18446f01427392a651fc4625de9ca9ab542823d103f0d2e6',
  'fences_sha256': 'd728b0b6d9cd870d463146700d29f5fd9cdc7a23dea0ce1d63bcaac35215165a',
  'job_sha256': 'e008eaa0726d248ce8eea8fe3284875dcffb0c2acbe319138089c122fb991a5a',
  'receipt_sha256': '447d9c3cc971b5a8723842f494ef8ce6dc4a43578b343a4b9547001f828b157f',
  'source_identity_sha256': '1bd5065dd117c16fbad4295f57dd9a29a98915180b78bbce217348c50d198514',
  'source_pair_sha256': 'c4c2f3dfcc013dca6904b1d2b1b6124f80b9a73542bd06d05aa3c22447c6a340'},
 {'drafts_sha256': 'b16287de3947e2563fcdadf14c71780610c165d6b5ae223807ee8d2c55994320',
  'edit_pair_sha256': None,
  'event_sha256': 'fffa08fce579c9928c9f6c10feec67bcb2368a5962c23cd21b50aaa4867f5535',
  'fences_sha256': '296906eaf4ed377008e95d17f08ef2a61428f78f24cebd808ce6cf092ef98bad',
  'job_sha256': 'a6183ee222277f91f218456ad715ac18c6ad3a9697f584dc710149070f8104d5',
  'receipt_sha256': '725437e06025dc0517238de10b36d7afdb79dd456d8069b99cec1b7815e81421',
  'source_identity_sha256': '1fcbb7b404a758b6107d93fff5e3919cb4663ab1b48d2cd84491f673780e4b2d',
  'source_pair_sha256': '55e9875a709d11c1ea6ac1df48b98c96b17aba6b49ae4f4222cce5435935c8da'},
 {'drafts_sha256': '3cdd9ed23bb6243872e6c96ecb9672b6166d1e9280bb3e74ebda0571ba4f05d4',
  'edit_pair_sha256': None,
  'event_sha256': 'c236c3d3c9f88a8103026a66c78bfa9e72d4f3335b5dd2bb81fba42d27fbee05',
  'fences_sha256': '8dee63e0150ff1827a20d20c7c91100c32c9128c45ef6f9abb8f06d500df2137',
  'job_sha256': '192128f41247c49d2b0b45e5eb995f07357e99b294fee46f1c27a6a8730d18a4',
  'receipt_sha256': 'b2e785cf0e551965952e2538c61eca6896719140ca8dee78a4c199479e4d0935',
  'source_identity_sha256': '2e6ab80cfe97bfe582562e23c3c55fc815d26f657388340bd731b78b8f2f5517',
  'source_pair_sha256': '5484ce133df0fe198fbd9cab77f76f3683f802169645ca5cd8daa1afde36fde4'},
 {'drafts_sha256': 'f10db0ab6bb2a2c9cc801afcf94bec78786783756948c14dd4798c2f26b893ac',
  'edit_pair_sha256': None,
  'event_sha256': '38076f05a8bd109c608642afa86aa18b7a92827b1d952843946e8a3144e3b853',
  'fences_sha256': '1dd99c4caae76517c9cf4a1db3075e7cfd7ee8cf0726a49c12634fc75c751566',
  'job_sha256': '11eae0115d253ca926ab3342e5321ae85efe98d6dec78779001db403a47ec3ac',
  'receipt_sha256': '19ea796bc01cba8bb703bbc7662a67b1e1a2214e7e15f5de95f9fafe595155b9',
  'source_identity_sha256': 'c0005df9479457e36d45116d7ec60eb673f57c17e9b3d12bc67dbc2dba65ba4f',
  'source_pair_sha256': '8a56f9c18533775a977002df5c9981f1ad76c21fb0c7a5386eb96b5fc71652c4'},
 {'drafts_sha256': 'b8c65535c8cbc3266f30ee57d5b89312b219c9b21725ba24a66bc2a99e488245',
  'edit_pair_sha256': None,
  'event_sha256': '9ac263e62c0dfcbee1794911a483d4d2d12bf29f1bd0d09ff5996802aec59af6',
  'fences_sha256': '72658fa9fc64ceb1999e1a2669dbcf0bf1412bb927f4962de1cf4105be6625ce',
  'job_sha256': 'b8785d82bb453aca7362f0f2d3fc13fccc52a55b82bf60237706ac9b5e1bbf0a',
  'receipt_sha256': 'aa48fe308ea1ee07f335b7c8c4a4bafa672a4223ee20aae6d2a49268cb0cf2ee',
  'source_identity_sha256': 'd63b2e85730b68be6934a22d8027fca1ecfb3ab975c6e4307426404501b17fb5',
  'source_pair_sha256': '329e2a9de924627b012e06d479991bbaf28223a070ee8685f14a9465a686da32'},
 {'drafts_sha256': 'd2f093e6cf188a66d5d781c2efecbba6661dbb349a26d0979088dfc852c1bd6c',
  'edit_pair_sha256': None,
  'event_sha256': '45de41cf317b460964db35ddd94a72742dc1c21de5384151a48e97fb365d23cf',
  'fences_sha256': 'ec63ba2f220ccb049a473de4602c5eff19de30ea46191610e7b415e0b4ee50b0',
  'job_sha256': '89f7c27a5ec59a0b2ae2ab41e9ddf2fda87e50c9fd179eb272cbd8c98102fa1e',
  'receipt_sha256': '5acd8b8f13112d8f6c846e185bcf52e55d74ffed93493b2ca187a72507b87e27',
  'source_identity_sha256': 'dfe5d074052dcd02d4b7982688ad383b1f7bf6410bd5d7fbead2f425361e7485',
  'source_pair_sha256': 'ed217d11461f6c1c6b2ed7137b98e65ad64d64e63d3bbf755d44ff1d91190982'})


def hashed(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def scope():
    if len(SCOPE)!=7 or {x['event_sha256'] for x in SCOPE}!=set(APPROVED_EVENTS):
        raise ValueError('RECOVERY7_EXACT_SCOPE_REQUIRED')
    for key in ('event_sha256','source_identity_sha256','source_pair_sha256','receipt_sha256','job_sha256','drafts_sha256','fences_sha256'):
        if any(not re.fullmatch('[0-9a-f]{64}',x[key]) for x in SCOPE):
            raise ValueError('RECOVERY7_INVALID_PIN')
    if any(len({x[k] for x in SCOPE})!=7 for k in ('event_sha256','source_identity_sha256','source_pair_sha256')):
        raise ValueError('RECOVERY7_DUPLICATE_IDENTITY')
    return SCOPE


def held_recovery(event_key,chat,message):
    pair=hashed(f'{chat}:{message}')
    for pin in scope():
        if (hashed(event_key)==pin['event_sha256'] or pair==pin['source_pair_sha256']
                or str(event_key).startswith('final:'+pin['source_identity_sha256']+':')):
            return dict(pin,_scope='RECOVERY7')
    return None


def deny(pin,channel):
    from six_unknown_receipt_hold import ReceiptReplayHeld
    reason='RECOVERY7_ORIGINAL_FAILURE_RECEIPT_HELD'
    logging.getLogger(__name__).warning('%s source=%s channel=%s attempt_created=false',reason,pin['source_identity_sha256'][:16],channel)
    raise ReceiptReplayHeld(reason)


def guard_direct(facts,order_id=None):
    # Success receipt wrapper must not bypass the original fenced source.
    from execution_safety import CURRENT
    job=(CURRENT.get() or {}).get('job') or {}
    if all(k in job for k in ('namespace','chat_id','message_id')):
        source=hashed(f"{job['namespace']}:{job['chat_id']}:{job['message_id']}")
        for pin in scope():
            if source==pin['source_identity_sha256']:deny(pin,'direct_receipt_context')


def guard_transport(endpoint,data):
    # Exact reply identity works even for an untagged direct ExtBot call.
    # Other endpoints (callbacks, documents, legitimate new notices) are untouched.
    if endpoint not in ('sendMessage','editMessageText'):return
    reply=data.get('reply_parameters') or {}
    if isinstance(reply,str):
        try:reply=json.loads(reply)
        except (ValueError,TypeError):reply={}
    msg=reply.get('message_id') if isinstance(reply,dict) else getattr(reply,'message_id',None)
    msg=msg or data.get('reply_to_message_id')
    if endpoint=='sendMessage':
        pin=held_recovery('',data.get('chat_id'),msg)
        if pin:deny(pin,'send_message_transport')
        from execution_safety import CURRENT
        job=(CURRENT.get() or {}).get('job') or {}
        if all(k in job for k in ('namespace','chat_id','message_id')):
            source=hashed(f"{job['namespace']}:{job['chat_id']}:{job['message_id']}")
            for pin in scope():
                if source==pin['source_identity_sha256']:deny(pin,'send_message_context')
    else:
        pair=hashed(f"{data.get('chat_id')}:{data.get('message_id')}")
        for pin in scope():
            if pin.get('edit_pair_sha256')==pair:deny(pin,'edit_message_transport')


def _rows(connection,sql,params=None):
    return [dict(x) for x in connection.execute(text(sql),params or {}).mappings().all()]


def partition_verified(unresolved,connection):
    """Read-only independent review; original P0 hash verifies the remainder."""
    pins={p['event_sha256']:p for p in scope()}
    selected={hashed(r['event_key']):r for r in unresolved if hashed(r['event_key']) in pins}
    if set(selected)!=set(pins):raise ValueError('RECOVERY7_EXACT_SET_CHANGED')
    for key,row in selected.items():
        pin=pins[key]
        if row['status']!='unknown' or row['attempts']!=1 or row['delivered_message_id'] is not None:
            raise ValueError('RECOVERY7_ORIGINAL_OUTCOME_CHANGED')
        args=dict(event=row['event_key'],chat=row['chat_id'],msg=row['source_message_id'])
        full=_rows(connection,'SELECT * FROM telegram_execution_receipts WHERE event_key=:event',args)
        if len(full)!=1 or digest(full[0])!=pin['receipt_sha256']:
            raise ValueError('RECOVERY7_ORIGINAL_ROW_CHANGED')
        if hashed(f"{row['chat_id']}:{row['source_message_id']}")!=pin['source_pair_sha256']:
            raise ValueError('RECOVERY7_SOURCE_CHANGED')
        jobs=_rows(connection,'SELECT * FROM telegram_order_jobs WHERE chat_id=:chat AND message_id=:msg',args)
        if len(jobs)!=1 or digest(jobs[0])!=pin['job_sha256']:
            raise ValueError('RECOVERY7_TERMINAL_JOB_CHANGED')
        job=jobs[0];args['ns']=job['namespace']
        if (job['state']!='TIMED_OUT' or job['lease_until'] is not None or job['lease_token'] is not None
                or hashed(f"{job['namespace']}:{job['chat_id']}:{job['message_id']}")!=pin['source_identity_sha256']):
            raise ValueError('RECOVERY7_SOURCE_NOT_QUIET')
        if _rows(connection,'SELECT id FROM orders WHERE source_chat_id=:chat AND source_message_id=:msg',args) or _rows(connection,'SELECT order_id FROM telegram_execution_commits WHERE namespace=:ns AND chat_id=:chat AND message_id=:msg',args):
            raise ValueError('RECOVERY7_BUSINESS_FACT_CHANGED')
        related=_rows(connection,'SELECT * FROM telegram_execution_receipts WHERE chat_id=:chat AND source_message_id=:msg ORDER BY event_key',args)
        if len(related)!=1 or related[0]['event_key']!=row['event_key']:
            raise ValueError('RECOVERY7_NEW_ATTEMPT_FOR_SOURCE')
        drafts=_rows(connection,'SELECT * FROM telegram_order_drafts WHERE namespace=:ns AND chat_id=:chat AND message_id=:msg ORDER BY token',args)
        fences=_rows(connection,'SELECT * FROM telegram_source_fences WHERE namespace=:ns AND chat_id=:chat AND message_id=:msg',args)
        if (digest(drafts)!=pin['drafts_sha256'] or digest(fences)!=pin['fences_sha256']
                or len(fences)!=1 or any(d['status']=='pending' for d in drafts)):
            raise ValueError('RECOVERY7_FENCE_OR_DRAFT_CHANGED')
    return [r for r in unresolved if hashed(r['event_key']) not in selected],list(selected)
