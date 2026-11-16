"""Customer Message.date -> full simulated Telegram ACK. Never a real ACK claim."""
import argparse,os,sys,socket,json,asyncio,tempfile,time,math
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--source',default=str(Path(__file__).resolve().parents[1]));p.add_argument('--scenario',choices=['single','multi','group4','same_customer4'],required=True);p.add_argument('--samples',type=int,default=20);p.add_argument('--arrival-seconds',type=float,default=2.0);p.add_argument('--output',required=True);a=p.parse_args()
sys.path[:0]=[str(Path(a.source)/'legacy_app'),str(Path(__file__).parent)]
os.environ.update(NEWBOT_ROLE='split-release-only',NEWBOT_SPLIT_MAPPINGS_ONLY='1',RAINBOW_DISABLE_BACKGROUND_WORKERS='1',BOT_TOKEN='123456789:offline')
socket.socket.connect=lambda *args,**kw:(_ for _ in ()).throw(RuntimeError('REAL_NETWORK_DISABLED'))
import models
models.db=None
from receipt_chain_support import ReceiptChain

def percentile(values):
 values=sorted(values)
 return {'n':len(values),**{f'p{int(q*100)}_ms':round(values[max(0,math.ceil(q*len(values))-1)],3) for q in [.5,.95,.99]}}
async def run(c):
 rows=[]
 if a.scenario=='single':
  for i in range(1,a.samples+1):
   c.prepare(i);row=await c.handle(i);rows.append(row)
 else:
  n=4 if a.scenario.endswith('4') else a.samples
  senders=[123456789] if a.scenario=='same_customer4' else [123456789+i for i in range(n)]
  for sid in senders:
   if sid!=123456789:c.customer(sid)
  gate=asyncio.Semaphore(4)
  async def one(i):
   async with gate:return await c.handle(i,pre_received=True)
  jobs=[]
  for i in range(1,n+1):
   c.prepare(i,sender=senders[(i-1)%len(senders)],chat=-1000000000001 if a.scenario.endswith('4') else -1000000000001-(i%10))
   assert await asyncio.to_thread(c.receive,i)
   jobs.append(asyncio.create_task(one(i)))
   if a.scenario=='multi':await asyncio.sleep(a.arrival_seconds)
  rows=await asyncio.gather(*jobs)
 n=len(rows);acks=[c.receipt_result(i) for i in range(1,n+1)]
 assert all(r['state']=='COMPLETED' for r in rows) and all(r['ack_at'] for r in acks)
 assert c.pipeline.counts()['orders']==n and c.pipeline.quantities()==(10000-n,10000-n)
 assert c.pipeline.db.engine.pool.checkedout()==0
 audit=[json.loads(line) for line in (c.path/'receipts.jsonl').read_text().splitlines()]
 assert sum(r['status']=='SENT' for r in audit)==n
 return {'provenance':'SIMULATED_HTTP_ACK_WITH_REAL_LIMITER_NOT_REAL_TELEGRAM','scenario':a.scenario,'arrival_seconds':a.arrival_seconds if a.scenario=='multi' else None,'source_clock':'Telegram Message.date integer seconds; rounding included',
         'network_latency_profile_seconds':[.382,.823,.649,1.322],'source_to_complete_ack':percentile([r['source_to_ack_ms'] for r in acks]),
         'persist_to_commit':percentile([r['persist_to_commit_ms'] for r in rows]),'queue':percentile([r['queue_ms'] for r in rows]),
         'max_queue_ms':max(r['queue_ms'] for r in rows),'last_ack_since_first_source_ms':(max(r['ack_at'] for r in acks)-min(r['source_date'] for r in acks))*1000,
         'peak_active_leases':c.peak_leases,'orders':n,'acknowledged_receipts':n,'fifo_stock_consistent':True,'pool_checked_out':0,'messages':acks,'stages':rows}
with tempfile.TemporaryDirectory(prefix='receipt-ack-bench-') as scratch:
 c=ReceiptChain(scratch,latency=[.382,.823,.649,1.322])
 try:r=asyncio.run(run(c));Path(a.output).write_text(json.dumps(r,indent=2));print('RECEIPT_ACK_RESULT',json.dumps({k:v for k,v in r.items() if k not in ('messages','stages')}),flush=True)
 finally:c.close()
