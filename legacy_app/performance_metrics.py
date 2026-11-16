"""Bounded private-free stage samples; provenance never pooled across modes."""
import os,json,fcntl,math,re,tempfile,logging
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal
from ai_settings import _write

MODES={'application','cloud_live','fixed_response','historical_reconstruction'}
PROVIDERS={'DeepSeek','AliyunBailian','OpenRouter','AliyunAddress','Local'}
STAGES={'MESSAGE_QUEUE','WORKER_CLAIM','WORKER_EXECUTION','QUEUE_TOTAL','AI_PERMIT_WAIT','AI_WORKER_QUEUE','ORDER_AI','CONTACT_AI','ORDER_MODEL_HTTP','CONTACT_MODEL_HTTP','FLAVOR_MODEL_HTTP','AI_ESTIMATED_COST','ADDRESS_SERVICE','ADDRESS_HTTP','ADDRESS_STRUCTUREADDRESS','ADDRESS_COMPLETEADDRESS','VALIDATOR','HTTP_CREATE','TELEGRAM_SEND','COMPLETE_RECEIPT','PROVIDER_HTTP','LOCAL_PARSER','API_TOTAL'}
STAGES.add('LEGACY_WORKER_WAIT')
STAGES.update(('PRODUCT_LIMIT_GUARD','REGION_SOURCE_GUARD','QUANTITY_SOURCE_GUARD'))
STAGES.update(('LOCAL_CPU_WAIT','LOCAL_CPU_WORK','LOCAL_LIGHT_WAIT','LOCAL_LIGHT_WORK','TELEGRAM_TURN_WAIT','TELEGRAM_PACING_WAIT','TELEGRAM_HTTP'))
STAGES.update(('PARSER_EXTRACT','CATALOG_MATCH','SOURCE_PREVALIDATION','DB_CONNECTION_WAIT','ORDER_COMMIT','PERSIST_TO_COMMIT'))

STAGES.update(('RECEIPT_GENERATE','RECEIPT_AUDIT_INTENT','RECEIPT_AUDIT_FINALIZE','ORDER_TO_RECEIPT_ACK'))
STAGES.update(('CATEGORY_PREPARE','REVIEWED_ALIAS_MATCH','VISIBLE_CATALOG','ORDER_PREVIEW',
               'CONTACT_RECOVERY','CURRENT_REGION','SOURCE_REGION_EVIDENCE',
               'SOURCE_QUANTITY_EVIDENCE','ORDER_WRITE_LOCK_WAIT'))
STAGES.update(('ADDRESS_QUOTA_PREWARM','ADDRESS_DAILY_ADMISSION'))

def path():return Path(os.getenv('RAINBOW_PERFORMANCE_PATH') or Path(__file__).with_name('performance_metrics.json'))
def mode():
    value=os.getenv('RAINBOW_PERFORMANCE_MODE','application')
    return value if value in MODES else 'application'

def _write_committed_api(target, value):
    """Same JSON bytes and durable atomic replacement, fewer Python writes.

    The caller retains the existing read/merge/write file lock. This never
    queues work or changes the persistence of execution/receipt audits.
    """
    encoded=json.dumps(value,ensure_ascii=False)
    fd,name=tempfile.mkstemp(prefix='.api-metrics-',dir=str(target.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name,0o600)
        os.replace(name,target)
    finally:
        if os.path.exists(name):os.unlink(name)


def record_samples(samples, *, committed_api=False):
    """Telemetry must never alter an order. Invalid samples are ignored."""
    safe=[]
    for row in samples:
        if not isinstance(row,dict) or row.get('mode') not in MODES or row.get('stage') not in STAGES:continue
        value=row.get('elapsed_ms')
        if type(value) not in (float,int) or not math.isfinite(value) or not 0<=value<=86400000:continue
        cost=None;kind=row.get('cost_kind','unknown')
        if kind in ('estimated','actual','simulated') and row.get('cost_cny') is not None:
            try:
                amount=Decimal(str(row['cost_cny']))
                if amount.is_finite() and 0<=amount<=100:cost=str(amount)
            except Exception:pass
        provider=row.get('provider') if row.get('provider') in PROVIDERS else 'Unknown'
        safe.append(dict(mode=row['mode'],provider=provider,stage=row['stage'],elapsed_ms=round(value,3),error=bool(row.get('error')),
            calls=max(0,min(100,row.get('calls',0))) if type(row.get('calls',0)) is int else 0,cost_cny=cost,cost_kind=kind if cost is not None else 'unknown',at=datetime.now(timezone.utc).isoformat()))
        if isinstance(row.get('evidence_id'),str) and re.fullmatch('[a-f0-9]{64}',row['evidence_id']):safe[-1]['evidence_id']=row['evidence_id']
        for key,size in (('trace_id',32),('source',16)):
            value=row.get(key)
            if isinstance(value,str) and re.fullmatch('[a-f0-9]{'+str(size)+'}',value):safe[-1][key]=value
    if not safe:return
    try:
        target=path();target.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(str(target)+'.lock',os.O_CREAT|os.O_RDWR,0o600)
        with os.fdopen(fd,'a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            data=json.loads(target.read_text()) if target.exists() else {'samples':[]}
            seen={r.get('evidence_id') for r in data['samples'] if isinstance(r,dict) and r.get('evidence_id')}
            unique=[]
            for r in safe:
                identity=r.get('evidence_id')
                if identity and identity in seen:continue
                if identity:seen.add(identity)
                unique.append(r)
            safe=unique
            writer=_write_committed_api if committed_api else _write
            writer(target,{'version':1,'samples':(data['samples']+safe)[-5000:]})
    except Exception as error:
        if committed_api:
            logging.getLogger(__name__).warning('API_PERFORMANCE_WRITE_FAILED kind=%s',type(error).__name__)

def record_sample(provenance,stage,elapsed_ms,**values):
    record_samples([dict(values,mode=provenance,stage=stage,elapsed_ms=elapsed_ms)])

def summary():
    try:rows=json.loads(path().read_text()).get('samples',[]) if path().exists() else []
    except Exception:return {'configuration_error':True,'groups':[]}
    def attribution(row):
        provider=row.get('provider') if row.get('provider') in PROVIDERS else 'Unknown'
        if provider!='Unknown':return provider,'recorded'
        if row.get('mode')=='application':return 'Local','stage_inferred'
        if row.get('stage','').startswith('ADDRESS_'):return 'AliyunAddress','stage_inferred'
        return 'Unknown','unavailable'
    groups={}
    for row in rows:
        if not isinstance(row,dict) or row.get('mode') not in MODES or row.get('stage') not in STAGES:continue
        if type(row.get('elapsed_ms')) not in (int,float) or not math.isfinite(row['elapsed_ms']):continue
        if not all(k in row for k in ('calls','cost_cny','cost_kind','error')):continue
        provider,provider_source=attribution(row)
        groups.setdefault((row['mode'],provider,provider_source,row['stage']),[]).append(row)
    result=[]
    for (provenance,provider,provider_source,stage),samples in sorted(groups.items()):
        elapsed=sorted(row['elapsed_ms'] for row in samples)
        percentile=lambda p:elapsed[max(0,math.ceil(len(elapsed)*p)-1)]
        charges={kind:sum(Decimal(row['cost_cny']) for row in samples if row['cost_kind']==kind and row['cost_cny'] is not None) for kind in ('estimated','actual','simulated')}
        result.append(dict(mode=provenance,provider=provider,provider_source=provider_source,stage=stage,samples=len(samples),mean_ms=round(sum(elapsed)/len(elapsed),3),p50_ms=percentile(.5),p95_ms=percentile(.95),calls=sum(row['calls'] for row in samples),errors=sum(row['error'] for row in samples),cost_cny={k:str(v) for k,v in charges.items()},cost_sample_count={k:sum(row['cost_kind']==k and row['cost_cny'] is not None for row in samples) for k in charges},unknown_cost_calls=sum(row['calls'] for row in samples if row['cost_cny'] is None)))
    return {'configuration_error':False,'groups':result,'retained_samples':len(rows),'max_samples':5000,'cost_note':'费用按实际、估算及模拟分列；未知费用不得视为0；仅统计保留窗口'}
