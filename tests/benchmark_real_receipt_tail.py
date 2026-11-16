"""Synthetic history + actual eight-worker/four-chat bound, no Telegram network."""
import argparse,asyncio,json,math,os,socket,sys,tempfile,time
from pathlib import Path
from datetime import datetime,timezone,timedelta

p=argparse.ArgumentParser()
p.add_argument('--source',default=str(Path(__file__).resolve().parents[1]))
p.add_argument('--samples',type=int,default=24)
p.add_argument('--history',type=int,default=750)
p.add_argument('--predecessors',type=int,default=300)
p.add_argument('--chats',type=int,default=1)
p.add_argument('--queue-history',type=int,default=0)
p.add_argument('--output',required=True)
p.add_argument('--metrics-write-probe',action='store_true')
a=p.parse_args()
sys.path[:0]=[str(Path(a.source)/'legacy_app'),str(Path(__file__).parent)]
os.environ.update(NEWBOT_ROLE='split-release-only',NEWBOT_SPLIT_MAPPINGS_ONLY='1',
 RAINBOW_DISABLE_BACKGROUND_WORKERS='1',BOT_TOKEN='123456789:offline',
 BOT_ACCOUNT_SWITCH_ID='123456789',BOT_ACCOUNT_SWITCH_AT='2026-01-01T00:00:00+00:00')
socket.socket.connect=lambda *v,**k:(_ for _ in ()).throw(RuntimeError('REAL_NETWORK_DISABLED'))
import models
models.db=None
from receipt_chain_support import ReceiptChain

def quantiles(v):
    v=sorted(v)
    return dict(n=len(v),**{f'p{int(q*100)}_ms':v[math.ceil(q*len(v))-1] for q in (.5,.95,.99)},max_ms=v[-1])

async def run(c):
    claim_times={};original=c.pipeline.queue.claim
    def claim(*args,**kwargs):
        job=original(*args,**kwargs)
        if job:claim_times[job['message_id']]=time.time()
        return job
    c.pipeline.queue.claim=claim
    c.pipeline.queue.message_chat_limit=4
    for i in range(1,a.samples+1):
        c.prepare(i,sender=123456789,chat=-1000000000001-(i%a.chats))
        assert await asyncio.to_thread(c.receive,i)
    started=time.perf_counter();gate=asyncio.Semaphore(8)
    async def one(i):
        async with gate:return await c.handle(i,pre_received=True)
    rows=await asyncio.gather(*(one(i) for i in range(1,a.samples+1)))
    elapsed=time.perf_counter()-started
    receipts=[c.receipt_result(i) for i in range(1,a.samples+1)]
    assert all(r['state']=='COMPLETED' for r in rows)
    assert all(x['ack_at'] for x in receipts)
    assert c.pipeline.counts()['orders']==a.history+a.predecessors+a.samples
    assert c.pipeline.quantities()==(10000-a.samples,10000-a.samples)
    assert c.pipeline.db.engine.pool.checkedout()==0
    audit=[json.loads(z) for z in (c.path/'receipts.jsonl').read_text().splitlines()]
    assert sum(x['status']=='SENT' for x in audit)==a.samples
    actual_queue=[];durable_ack=[];source_ack=[]
    for i,receipt in enumerate(receipts,1):
        src=__import__('hashlib').sha256(f'shared:{-1000000000001-(i%a.chats)}:{i}'.encode()).hexdigest()[:16]
        durable=c.clocks[src]['QUEUE_DURABLE']['durable_at']
        actual_queue.append((claim_times[i]-durable)*1000)
        durable_ack.append((receipt['ack_at']-durable)*1000)
        source_ack.append(receipt['source_to_ack_ms'])
    stages={}
    for name in ('ADDRESS_QUOTA_PREWARM','ADDRESS_DAILY_ADMISSION','LOCAL_PARSER','HTTP_CREATE','TELEGRAM_HTTP','POST_COMMIT_LEARNING'):
        values=[sum(v for n,v in r['stages'] if n==name) for r in rows if any(n==name for n,v in r['stages'])]
        if values:stages[name]=quantiles(values)
    api_stages={}
    for name in ('ADDRESS_QUOTA_PREWARM','ADDRESS_DAILY_ADMISSION','ORDER_WRITE_LOCK_WAIT'):
        values=[sum(end-start for label,start,end in spans if label==name)
                for spans in c.api_spans.values() if any(z[0]==name for z in spans)]
        if values:api_stages[name]=quantiles(values)
    return dict(provenance='SYNTHETIC_ISOLATED_ACK_NOT_REAL_TELEGRAM',history=a.history,legacy_predecessors=a.predecessors,
      catalog='existing 536 synthetic reference rows and 1534 aliases',samples=a.samples,queue_history=a.queue_history,
      workers=8,chat_limit=4,chats=a.chats,same_customer=True,arrival='all messages durably queued before burst',
      source_to_ack=quantiles(source_ack),durable_to_ack=quantiles(durable_ack),actual_worker_queue=quantiles(actual_queue),
      writer_hold=quantiles([r['write_lock_hold_ms'] for r in rows]),elapsed_seconds=elapsed,
      peak_active_leases=c.peak_leases,orders=a.samples,acks=len(receipts),fifo_consistent=True,pool_checked_out=0,stages=stages,api_stages=api_stages,
      messages=receipts)

with tempfile.TemporaryDirectory(prefix='real-tail-synthetic-') as scratch:
    c=ReceiptChain(scratch,latency=[.382,.823,.649,1.322])
    probe=None
    if a.metrics_write_probe:
        from metrics_write_probe import Probe
        probe=Probe(c)
    try:
        # Today's completed orders retain pending raw status just as observed
        # in production. They are unrelated addresses, no stock/ledger effects.
        now=datetime.now(timezone.utc).replace(tzinfo=None)
        with c.pipeline.db.SessionLocal() as s:
            for i in range(a.history):
                s.add(models.Order(user_id=c.pipeline.user.id,order_number=f'HISTORY-{i}',
                    recipient_name='隔离',recipient_phone='13800000000',
                    recipient_address=f'北京市朝阳区建国路{100000+i}号',created_at=now,recorded_at=now,
                    status='pending',total_amount=0,total_shipping_fee=0,grand_total=0))
            for i in range(a.predecessors):
                s.add(models.Order(user_id=c.pipeline.user.id,order_number=f'LEGACY-PENDING-{i}',
                    recipient_name='隔离',recipient_phone='13800000000',
                    recipient_address=f'北京市朝阳区建国路{200000+i}号',created_at=now-timedelta(days=2),
                    status='pending',total_amount=0,total_shipping_fee=0,grand_total=0))
            s.commit()
        # Completed queue history exercises the same claim query against a
        # busy historical namespace; these synthetic rows cannot be replayed.
        if a.queue_history:
            q=c.pipeline.queue;stamp=time.time()-86400
            encrypted=q.cipher.encrypt(b'{"message":{"text":"synthetic-terminal-history"}}').decode()
            history=[('shared',str(-1000000000001-(i%4)),100000+i,'123456789',encrypted,
                      'synthetic-terminal-history',stamp,'COMPLETED',stamp)
                     for i in range(a.queue_history)]
            with c.pipeline.db.engine.begin() as connection:
                connection.exec_driver_sql('INSERT INTO telegram_order_jobs(namespace,chat_id,message_id,sender_id,payload,fingerprint,received_at,state,started_at) VALUES(?,?,?,?,?,?,?,?,?)',history)
        # Identical full bounded telemetry file in both sources.
        sample=dict(mode='application',provider='Local',stage='LOCAL_PARSER',elapsed_ms=100,error=False,
          calls=0,cost_cny=None,cost_kind='unknown',at='2026-01-01T00:00:00+00:00',trace_id='f'*32,source='0'*16)
        Path(os.environ['RAINBOW_PERFORMANCE_PATH']).write_text(json.dumps({'version':1,'samples':[sample]*5000}))
        result=asyncio.run(run(c))
        if probe is not None:result['metrics_write_probe']=probe.result()
        Path(a.output).write_text(json.dumps(result,indent=2))
        print('REAL_TAIL_SYNTHETIC',json.dumps({k:v for k,v in result.items() if k!='messages'}),flush=True)
    finally:
        if probe is not None:probe.close()
        c.close()
