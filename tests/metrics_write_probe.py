"""Same-load numeric timings only; both sources get identical wrappers."""
import time,math,threading

def quantiles(values):
 values=sorted(values)
 return dict(n=len(values),**{f'p{int(q*100)}_ms':values[math.ceil(q*len(values))-1] for q in (.5,.95,.99)},max_ms=values[-1]) if values else dict(n=0)

class Probe:
 def __init__(self,c):
  import performance_metrics,bot
  self.c=c;self.events=[];self.guard=threading.Lock()
  self.record=performance_metrics.record_samples;self.client=bot.api_client;self.request=self.client._make_request
  owner=self
  def identity():
   from order_trace import CURRENT
   return (CURRENT.get() or {}).get('trace_id')
  def record(samples,*a,**kw):
   rows=list(samples);kind='API' if any(r.get('stage')=='API_TOTAL' for r in rows) else 'BOT';trace=identity();start=time.time();cpu=time.thread_time()
   try:return owner.record(rows,*a,**kw)
   finally:
    with owner.guard:owner.events.append(dict(kind=kind,trace_id=trace,start=start,end=time.time(),cpu_ms=(time.thread_time()-cpu)*1000))
  def request(method,endpoint,*a,**kw):
   if endpoint!='/api/orders/create':return owner.request(method,endpoint,*a,**kw)
   trace=identity();start=time.time()
   try:return owner.request(method,endpoint,*a,**kw)
   finally:
    with owner.guard:owner.events.append(dict(kind='HTTP_CREATE',trace_id=trace,start=start,end=time.time()))
  performance_metrics.record_samples=record;self.client._make_request=request
 def result(self):
  rows=[]
  for source,trace in self.c.trace_ids.items():
   e=[r for r in self.events if r['trace_id']==trace];api=next(r for r in e if r['kind']=='API');http=next(r for r in e if r['kind']=='HTTP_CREATE');bot=next(r for r in e if r['kind']=='BOT');cl=self.c.clocks[source];commit=cl['TRANSACTION_COMMITTED']['committed_at']
   rows.append(dict(source=source,api_stats_write_ms=(api['end']-api['start'])*1000,api_stats_cpu_ms=api['cpu_ms'],http_create_ms=(http['end']-http['start'])*1000,commit_to_http_return_ms=(http['end']-commit)*1000,bot_stats_write_ms=(bot['end']-bot['start'])*1000,committed_at=commit,http_return_at=http['end'],api_stats_start=api['start'],api_stats_end=api['end']))
  return dict(provenance='SYNTHETIC_ACK_NUMERIC_ONLY',samples=rows,quantiles={k:quantiles([r[k] for r in rows]) for k in ('api_stats_write_ms','api_stats_cpu_ms','http_create_ms','commit_to_http_return_ms','bot_stats_write_ms')})
 def close(self):
  import performance_metrics
  performance_metrics.record_samples=self.record;self.client._make_request=self.request
