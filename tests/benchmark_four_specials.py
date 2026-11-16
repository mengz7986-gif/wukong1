"""In-process isolated latency evidence; never a production load generator."""
import argparse,json,os,sys,tempfile,time,math,socket
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--source',default=str(Path(__file__).resolve().parents[1]));p.add_argument('--samples',type=int,default=40);p.add_argument('--output',required=True);args=p.parse_args()
source=Path(args.source).resolve();sys.path.insert(0,str(source/'legacy_app'));sys.path.insert(0,str(Path(__file__).parent))
os.environ.update(NEWBOT_ROLE='split-release-only',NEWBOT_SPLIT_MAPPINGS_ONLY='1',RAINBOW_DISABLE_BACKGROUND_WORKERS='1',BOT_TOKEN='123456789:offline')
os.environ.pop('NEWBOT_INGESTION_POLICY_PATH',None)
def deny(*a,**kw):raise RuntimeError('OFFLINE_NETWORK_DENIED')
socket.create_connection=deny;socket.socket.connect=deny
from four_specials_support import Pipeline
from order_parser import OrderParser
from rule_dictionary import runtime_snapshot
from request_memo import request_memo_scope
from bot import OrderBot
from order_trace import CURRENT

def summarize(values):
 values=sorted(values);pick=lambda q:values[max(0,math.ceil(len(values)*q)-1)]
 return dict(n=len(values),p50_ms=pick(.5),p95_ms=pick(.95),p99_ms=pick(.99))
rows=[]
with tempfile.TemporaryDirectory(prefix='four-specials-bench-') as scratch:
 os.chdir(scratch);pipeline=Pipeline(Path(scratch)/'runtime');pipeline.add_representative_reference();bot=OrderBot.__new__(OrderBot)
 for i in range(args.samples):
  mid=i+1;address=f'北京市朝阳区建国路{i+1}号'
  receipt_started=time.time();assert pipeline.receive(mid,address=address);durable=time.time()
  job=None;deadline=time.monotonic()+2
  while job is None and time.monotonic()<deadline:
   job=pipeline.queue.claim()
   if job is None:time.sleep(.01)
  assert job is not None;claimed=time.time()
  record={'trace_id':str(mid).zfill(32),'started':time.perf_counter(),'started_wall':time.time(),'stages':[],'ttfr_ms':None}
  token=CURRENT.set(record)
  try:
   parse_start=time.time()
   with runtime_snapshot(),request_memo_scope():parsed=OrderParser.extract_order_info(pipeline.payload(mid,address=address)['message']['text'],pipeline.catalog)
   parse_end=time.time();assert parsed and parsed.get('items')
   matched_start=time.time()
   matches=[bot._match_product(name,pipeline.catalog) for name,qty in parsed['items']];assert all(matches)
   matched_end=time.time()
   with runtime_snapshot(),request_memo_scope():order=pipeline.create(mid,job=job,address=address)
   committed=time.time();assert order is not None
   assert pipeline.queue.finish(job,'COMPLETED')
   # The real network is excluded. This clock marks where an external sender
   # would be invoked; no Telegram receipt is claimed as delivered.
   receipt_boundary=time.time()
   rows.append(dict(message=mid,telegram_receive_at=receipt_started,durable_at=durable,worker_claim_at=claimed,
     parser_start_at=parse_start,parser_end_at=parse_end,catalog_match_start_at=matched_start,catalog_match_end_at=matched_end,
     transaction_committed_at=committed,receipt_sender_boundary_at=receipt_boundary,
     parser_ms=(parse_end-parse_start)*1000,persist_to_commit_ms=(committed-durable)*1000,
     queue_ms=(claimed-durable)*1000,stages=record['stages']))
  finally:CURRENT.reset(token);pipeline.release(job)
  if (i+1)%5==0:print('committed',i+1,'latest_ms',round(rows[-1]['persist_to_commit_ms'],2),flush=True)
 assert pipeline.counts()['orders']==args.samples and pipeline.quantities()==(10000-args.samples,10000-args.samples)
 checked_out=pipeline.db.engine.pool.checkedout()
 result={'provenance':'isolated_in_process_synthetic_normal_load_single_worker_one_message_in_flight',
   'network':'denied; external receipt not measured','catalog_rows':len(pipeline.catalog),'dictionary_bytes':(pipeline.path/'dictionary.json').stat().st_size,
   'parser':summarize([r['parser_ms'] for r in rows]),'persist_to_commit':summarize([r['persist_to_commit_ms'] for r in rows]),'queue':summarize([r['queue_ms'] for r in rows]),
   'pool_checked_out':checked_out,'fifo_stock_consistent':True,'orders_committed':args.samples,'rows':rows}
 pipeline.close()
Path(args.output).write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='rows'}),flush=True)
