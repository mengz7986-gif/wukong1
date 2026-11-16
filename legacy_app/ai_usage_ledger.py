"""Privacy-free AI attempt ledger; prompts, responses and credentials are forbidden."""
from __future__ import annotations
import fcntl,json,os,tempfile,uuid
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

BEIJING=ZoneInfo('Asia/Shanghai');MAX_ROWS=10000
PROVIDERS={'DeepSeek','OpenRouter','AliyunBailian'}
MODELS={'deepseek-flash','deepseek-chat','deepseek-reasoner','qwen-plus','qwen-turbo'}
KINDS={'ORDER_PARSE','CONTACT_PARSE','ORDER_FLAVOR_CANDIDATE','DAILY_LEARNING','TEST_CONNECTION','FAILURE_ANALYSIS'}
PROVENANCE={'cloud_live','fixed_response','historical_reconstruction'}
PRICE_PROFILES={'deepseek-flash-v4.1-observed-2026-10-16':{
 'provider':'DeepSeek','model':'deepseek-flash','currency':'CNY','unit':'1M_tokens',
 'observed_at':'2026-10-16T04:06:00+08:00','historical_effective_from':None,
 'source':'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
 'offpeak':{'cache_hit_input':'0.02','cache_miss_input':'1','output':'4'},
 'peak':{'cache_hit_input':'0.04','cache_miss_input':'2','output':'8'},
 'peak_beijing':'工作日09:00-12:00、14:00-18:00；中国法定假日除外','holiday_calendar_connected':False,
 'parallel_published_usd_rates_not_converted':{
  'offpeak':{'cache_hit_input':'0.003','cache_miss_input':'0.15','output':'0.6'},
  'peak':{'cache_hit_input':'0.006','cache_miss_input':'0.3','output':'1.2'}}}}

def path():return Path(os.getenv('RAINBOW_AI_USAGE_PATH') or Path(__file__).with_name('ai_usage_ledger.json'))
def _stamp(value):
 try:
  stamp=datetime.fromisoformat(str(value).replace('Z','+00:00'))
  return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
 except Exception:return None
def _read():
 target=path()
 if not target.exists():return {'version':2,'attempts':[],'coverage_started_at':None,'ever_truncated':False,'official_actual':{'status':'UNAVAILABLE'}}
 value=json.loads(target.read_text(encoding='utf-8'))
 if isinstance(value,dict) and isinstance(value.get('calls'),list) and 'attempts' not in value:
  value['attempts']=[dict(row,provenance='historical_reconstruction') for row in value.pop('calls')];value['version']=2
 if not isinstance(value,dict) or not isinstance(value.get('attempts'),list):raise ValueError('AI usage ledger invalid')
 return value
def _write(value):
 target=path();target.parent.mkdir(parents=True,exist_ok=True);fd,name=tempfile.mkstemp(prefix='.ai-usage-',dir=target.parent)
 try:
  with os.fdopen(fd,'w',encoding='utf-8') as out:
   json.dump(value,out,ensure_ascii=False,separators=(',',':'));out.flush();os.fsync(out.fileno())
  os.chmod(name,0o600);os.replace(name,target)
 finally:
  if os.path.exists(name):os.unlink(name)
def _usage(payload):
 raw=payload.get('usage') if isinstance(payload,dict) else None
 keys=('prompt_tokens','completion_tokens','total_tokens','cache_hit_input_tokens','cache_miss_input_tokens')
 if not isinstance(raw,dict):return ({k:None for k in keys},'MISSING')
 def num(key):
  value=raw.get(key);return value if type(value) is int and value>=0 else None
 details=raw.get('prompt_tokens_details') if isinstance(raw.get('prompt_tokens_details'),dict) else {}
 values={'prompt_tokens':num('prompt_tokens'),'completion_tokens':num('completion_tokens'),'total_tokens':num('total_tokens'),
  'cache_hit_input_tokens':num('prompt_cache_hit_tokens'),'cache_miss_input_tokens':num('prompt_cache_miss_tokens')}
 if values['cache_hit_input_tokens'] is None:
  cached=details.get('cached_tokens');values['cache_hit_input_tokens']=cached if type(cached) is int and cached>=0 else None
 if any(values[k] is None for k in ('prompt_tokens','completion_tokens','total_tokens')):return values,'INCOMPLETE'
 if values['prompt_tokens']+values['completion_tokens']!=values['total_tokens']:return values,'INCONSISTENT'
 hit,miss=values['cache_hit_input_tokens'],values['cache_miss_input_tokens']
 if (hit is not None and hit>values['prompt_tokens']) or (miss is not None and miss>values['prompt_tokens']):return values,'INCONSISTENT'
 if hit is not None and miss is not None and hit+miss!=values['prompt_tokens']:return values,'INCONSISTENT'
 return values,'VALID'
def _estimate(profile,usage,status,when):
 if not profile:return {'status':'PRICE_PROFILE_MISSING','currency':None,'low':None,'high':None}
 stamp,observed=_stamp(when),_stamp(profile['observed_at'])
 if status!='VALID':return {'status':'UNSTATISTICAL','currency':profile['currency'],'low':None,'high':None}
 if not stamp or not observed or stamp<observed:return {'status':'PRICE_APPLICABILITY_UNKNOWN','currency':profile['currency'],'low':None,'high':None}
 prompt,output=usage['prompt_tokens'],usage['completion_tokens'];hit,miss=usage['cache_hit_input_tokens'],usage['cache_miss_input_tokens']
 if hit is not None:splits=[(hit,prompt-hit)]
 elif miss is not None:splits=[(prompt-miss,miss)]
 else:splits=[(prompt,0),(0,prompt)]
 million=Decimal(1000000)
 def cost(rates,pair):return (Decimal(rates['cache_hit_input'])*pair[0]+Decimal(rates['cache_miss_input'])*pair[1]+Decimal(rates['output'])*output)/million
 amounts=[cost(profile[period],pair) for period in ('offpeak','peak') for pair in splits]
 return {'status':'ESTIMATED_RANGE','currency':profile['currency'],'low':str(min(amounts)),'high':str(max(amounts)),'cache_split_known':len(splits)==1,'official_actual':False}

def record_attempt(*,settings,kind,provenance,outcome,http_status=None,response=None,logical_request_id=None,trace_id=None,attempt_id=None,at=None):
 if provenance not in PROVENANCE:raise ValueError('usage provenance invalid')
 stamp=_stamp(at or datetime.now(timezone.utc).isoformat())
 if not stamp or stamp>datetime.now(timezone.utc)+timedelta(minutes=5):raise ValueError('usage timestamp invalid')
 provider=settings.get('provider') if settings.get('provider') in PROVIDERS else 'Unknown'
 model=settings.get('model') if settings.get('model') in MODELS else 'Unknown';kind=kind if kind in KINDS else 'OTHER'
 try:payload=response.json() if response is not None else None
 except Exception:payload=None
 usage,status=_usage(payload)
 if provenance!='cloud_live' and status=='VALID':status='SIMULATED_USAGE'
 profile=next((p for p in PRICE_PROFILES.values() if p['provider']==provider and p['model']==model),None)
 estimate=_estimate(profile,usage,'VALID' if status=='VALID' else status,stamp.isoformat())
 valid_id=lambda value:isinstance(value,str) and len(value)==32 and all(c in '0123456789abcdef' for c in value)
 aid=attempt_id if valid_id(attempt_id) else uuid.uuid4().hex;lid=logical_request_id if valid_id(logical_request_id) else None;tid=trace_id if valid_id(trace_id) else None
 allowed={'SUCCESS','TIMEOUT','NETWORK_ERROR','RATE_LIMIT','AUTH_ERROR','CREDIT_ERROR','MODEL_ERROR','PROVIDER_ERROR','INVALID_RESPONSE','OTHER_ERROR'}
 row={'at':stamp.isoformat(),'attempt_id':aid,'logical_request_id':lid,'trace_id':tid,'provider':provider,'model':model,'kind':kind,'provenance':provenance,
  'outcome':outcome if outcome in allowed else 'OTHER_ERROR','http_status':http_status if type(http_status) is int and 100<=http_status<=599 else None,
  'usage':usage,'usage_status':status,'estimate':estimate}
 target=path();target.parent.mkdir(parents=True,exist_ok=True)
 with open(str(target)+'.lock','a') as lock:
  os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX);value=_read();rows=value.setdefault('attempts',[])
  if any(existing.get('attempt_id')==aid for existing in rows):return row
  if value.get('coverage_started_at') is None:value['coverage_started_at']=row['at']
  if len(rows)>=MAX_ROWS:value['ever_truncated']=True
  rows.append(row);value['attempts']=rows[-MAX_ROWS:];value['price_profiles']=PRICE_PROFILES
  value.setdefault('official_actual',{'status':'UNAVAILABLE','reason':'No official itemized billing API is connected; estimates are not official charges.'});_write(value)
 return row
def record_response(*,settings,kind,response,provenance,request_id=None,at=None):
 return record_attempt(settings=settings,kind=kind,response=response,provenance=provenance,outcome='SUCCESS',http_status=getattr(response,'status_code',200),logical_request_id=request_id,at=at)

def associate_commit(*,trace_id,commit_fingerprint,order_count=1):
 """Attach all physical attempts in one order trace to one committed group."""
 valid=lambda value,size:isinstance(value,str) and len(value)==size and all(c in '0123456789abcdef' for c in value)
 if not valid(trace_id,32) or not valid(commit_fingerprint,64):raise ValueError('commit association invalid')
 if type(order_count) is not int or not 1<=order_count<=100:raise ValueError('order count invalid')
 target=path()
 with open(str(target)+'.lock','a') as lock:
  os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX);value=_read();matched=0
  for row in value.get('attempts',[]):
   if row.get('trace_id')==trace_id:
    row['commit_fingerprint']=commit_fingerprint;row['committed_order_count']=order_count;matched+=1
  if matched:_write(value)
 return {'matched_attempts':matched,'commit_fingerprint':commit_fingerprint,'committed_order_count':order_count}

def summary(now=None):
 now=now or datetime.now(timezone.utc)
 if now.tzinfo is None:raise ValueError('summary time must be timezone-aware')
 local=now.astimezone(BEIJING);stored=_read();valid=[];invalid=0
 for row in stored.get('attempts',[]):
  stamp=_stamp(row.get('at')) if isinstance(row,dict) else None
  if not stamp or stamp>now.astimezone(timezone.utc)+timedelta(minutes=5):invalid+=1
  else:valid.append((row,stamp.astimezone(BEIJING)))
 def selected(period):return [r for r,s in valid if (s.date()==local.date() if period=='today' else (s.year,s.month)==(local.year,local.month))]
 def rolling(delta):
  cutoff=now.astimezone(timezone.utc)-delta
  return [r for r,s in valid if s.astimezone(timezone.utc)>=cutoff]
 def aggregate(rows):
  result={}
  for provenance in ('cloud_live','fixed_response','historical_reconstruction'):
   subset=[r for r in rows if r.get('provenance')==provenance];fields={k:{'known_sum':0,'known_calls':0,'unknown_calls':0} for k in ('prompt_tokens','completion_tokens','total_tokens','cache_hit_input_tokens','cache_miss_input_tokens')};estimates={}
   for row in subset:
    for key,bucket in fields.items():
     value=(row.get('usage') or {}).get(key)
     if type(value) is int:bucket['known_sum']+=value;bucket['known_calls']+=1
     else:bucket['unknown_calls']+=1
    estimate=row.get('estimate') or {};currency=estimate.get('currency')
    if currency and estimate.get('low') is not None and estimate.get('high') is not None:
     bucket=estimates.setdefault(currency,{'low':Decimal(0),'high':Decimal(0),'calls':0});bucket['low']+=Decimal(str(estimate['low']));bucket['high']+=Decimal(str(estimate['high']));bucket['calls']+=1
   result[provenance]={'attempts':len(subset),'successful_attempts':sum(r.get('outcome')=='SUCCESS' for r in subset),'tokens':fields,
    'outcomes':{name:sum(r.get('outcome')==name for r in subset) for name in sorted({str(r.get('outcome')) for r in subset})},
    'estimates':{currency:{'low':str(b['low']),'high':str(b['high']),'calls':b['calls']} for currency,b in estimates.items()},
    'unpriced_or_invalid_usage_attempts':sum((r.get('estimate') or {}).get('low') is None for r in subset)}
  return {'attempts':len(rows),'by_provenance':result}
 groups={}
 for row,_stamp_value in valid:
  if row.get('provenance')!='cloud_live' or not row.get('commit_fingerprint'):continue
  group=groups.setdefault(row['commit_fingerprint'],{'attempts':0,'committed_order_count':row.get('committed_order_count') or 1,'estimates':{}});group['attempts']+=1
  estimate=row.get('estimate') or {};currency=estimate.get('currency')
  if currency and estimate.get('low') is not None and estimate.get('high') is not None:
   bucket=group['estimates'].setdefault(currency,{'low':Decimal(0),'high':Decimal(0)})
   bucket['low']+=Decimal(str(estimate['low']));bucket['high']+=Decimal(str(estimate['high']))
 for group in groups.values():
  group['estimates']={c:{'group_low':str(b['low']),'group_high':str(b['high']),
   'per_order_allocation_low':str(b['low']/group['committed_order_count']),
   'per_order_allocation_high':str(b['high']/group['committed_order_count'])} for c,b in group['estimates'].items()}
 coverage_start=_stamp(stored.get('coverage_started_at'));earliest=min((stamp for _,stamp in valid),default=None);latest=max((stamp for _,stamp in valid),default=None)
 coverage={'status':'NO_LEDGER' if not coverage_start else ('TRUNCATED' if stored.get('ever_truncated') else 'PARTIAL_SINCE_START'),
  'coverage_started_at':coverage_start.isoformat() if coverage_start else None,
  'earliest_retained_at':earliest.isoformat() if earliest else None,'latest_retained_at':latest.isoformat() if latest else None,
  'retained_attempts':len(valid),'ever_truncated':bool(stored.get('ever_truncated')),
  'warning':'未覆盖的历史调用、Token和费用不在统计中；当前值只是已记录范围。'}
 def rolling_coverage(delta):
  cutoff=now.astimezone(timezone.utc)-delta
  if not coverage_start:return 'NO_LEDGER'
  if stored.get('ever_truncated'):return 'TRUNCATED'
  return 'COMPLETE' if coverage_start<=cutoff else 'PARTIAL_SINCE_START'
 rolling_24h=aggregate(rolling(timedelta(hours=24)));rolling_24h['coverage_status']=rolling_coverage(timedelta(hours=24))
 rolling_7d=aggregate(rolling(timedelta(days=7)));rolling_7d['coverage_status']=rolling_coverage(timedelta(days=7))
 return {'timezone':'Asia/Shanghai','today':aggregate(selected('today')),'month':aggregate(selected('month')),
  'rolling_24h':rolling_24h,'rolling_7d':rolling_7d,
  'retained_attempts':len(valid),'invalid_or_future_rows':invalid,'coverage':coverage,
  'committed_order_groups':groups,'order_cost_note':'One trace is one committed group. Split orders share group cost; per-order values are equal allocation, not extra provider calls. Failed/uncommitted attempts remain in period totals.',
  'official_actual':stored.get('official_actual') or {'status':'UNAVAILABLE'},'price_profiles':stored.get('price_profiles') or PRICE_PROFILES,
  'cost_note':'Only cloud_live provider usage is production evidence. Estimates are not official charges; simulated and historical rows are separate.'}
