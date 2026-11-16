"""Complete existing Bot -> HTTP -> source guards -> transaction; offline Telegram."""
import argparse,os,sys,tempfile,asyncio,json,math,time,socket
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--source',default=str(Path(__file__).resolve().parents[1]));p.add_argument('--samples',type=int,default=24);p.add_argument('--concurrency',type=int,default=1);p.add_argument('--output',required=True);args=p.parse_args()
sys.path[:0]=[str(Path(args.source)/'legacy_app'),str(Path(__file__).parent)]
os.environ.update(NEWBOT_ROLE='split-release-only',NEWBOT_SPLIT_MAPPINGS_ONLY='1',RAINBOW_DISABLE_BACKGROUND_WORKERS='1',BOT_TOKEN='123456789:offline')
# ASGI adapter performs real HTTP parsing/routing in process, no sockets.
socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('OFFLINE_NETWORK_DENIED'))
import models
models.db=None
from full_chain_support import FullChain

def summary(v):
 v=sorted(v)
 return {'n':len(v),**{f'p{int(q*100)}_ms':round(v[max(0,math.ceil(len(v)*q)-1)],3) for q in [.5,.95,.99]}}
async def run(f):
 gate=asyncio.Semaphore(args.concurrency)
 async def one(mid):
  async with gate:return await f.handle(mid)
 return await asyncio.gather(*(one(i+1) for i in range(args.samples)))
with tempfile.TemporaryDirectory(prefix='full-chain-bench-') as d:
 f=FullChain(d)
 try:
  rows=asyncio.run(run(f));assert all(r['state']=='COMPLETED' and r['persist_to_commit_ms'] is not None for r in rows)
  assert f.pipeline.counts()['orders']==args.samples and f.pipeline.quantities()==(10000-args.samples,10000-args.samples)
  assert len(f.sent)==args.samples
  r={'boundary':'durable queue -> real Bot.handle_message -> APIClient -> HTTP ASGI independent Context -> real FastAPI/source guards/FIFO/transaction -> fake Telegram ACK','samples':args.samples,'concurrency':args.concurrency,'parser':summary([ms for r in rows for ms in r['parser_ms']]),'persist_to_commit':summary([r['persist_to_commit_ms'] for r in rows]),'queue':summary([r['queue_ms'] for r in rows]),'write_lock_hold':summary([r['write_lock_hold_ms'] for r in rows if r['write_lock_hold_ms'] is not None]),'peak_active_leases':f.peak_leases,'pool_checkout':summary(f.pool_wait),'sql_count':len(f.sql),'http_count':len(f.calls),'orders':args.samples,'receipt_acks':len(f.sent),'fifo_stock_consistent':True,'pool_checked_out':f.pipeline.db.engine.pool.checkedout(),'rows':rows}
  Path(args.output).write_text(json.dumps(r,indent=2));print('FULL_CHAIN_RESULT',json.dumps({k:v for k,v in r.items() if k!='rows'}),flush=True)
 finally:f.close()
