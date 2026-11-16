"""Bounded cloud-primary address entry with source-preserving safety guards."""
import json,time,re,threading,fcntl,os
import uuid
from contextvars import ContextVar
from datetime import datetime,timezone,timedelta
from aliyun_address_provider import AliyunAddressCandidate,GeographicProof
import aliyun_address_settings as settings

_slots=threading.BoundedSemaphore(2)
_LAST_ADMISSION=ContextVar('aliyun_address_admission',default=None)

def diagnostic(phase,code,error=None,*,calls=None,http_status=None,parser_state=None,final_state=None):
    """Fixed labels only; provider exceptions may contain private addresses."""
    import logging
    try:
        from network_safety import identity
        row=dict(identity(),phase=phase,code=code)
        row.update(calls=calls,aliyun_called=bool(calls) if calls is not None else None,
            http_status=http_status if type(http_status) is int and 100<=http_status<=599 else None,
            local_quota_exhausted=code=='ADDRESS_BUDGET_EXHAUSTED',parser_state=parser_state,final_state=final_state)
        if error is not None:
            row['timeout']=isinstance(error,TimeoutError) or type(error).__name__ in ('ReadTimeout','ConnectTimeout','ReadTimeoutError')
            row['error_kind']=type(error).__name__
        logging.getLogger(__name__).info('ADDRESS_CHAIN_DIAGNOSTIC %s',json.dumps(row,sort_keys=True))
    except Exception:pass  # Observability cannot change validation or billing.

def provider_failure_reason(error):
    # Classify only an explicit provider code, never arbitrary response text
    # (which may contain the customer's submitted address).
    getter=getattr(error,'get_error_code',None)
    try:code=getter() if callable(getter) else None
    except Exception:code=None
    if code in {'InsufficientBalance','Forbidden.InsufficientBalance','InvalidAccountStatus.NotEnoughBalance','Arrearage'}:
        return 'ADDRESS_ACCOUNT_BALANCE_INSUFFICIENT'
    if code=='Throttling.User':return 'ADDRESS_PROVIDER_RATE_LIMITED'
    return 'ADDRESS_SERVICE_FAILED'

def transport(action,raw,cfg):
    if action not in ('StructureAddress','CompleteAddress'):raise ValueError('ADDRESS_ACTION_INVALID')
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkaddress_purification.request.v20191118.StructureAddressRequest import StructureAddressRequest
    from aliyunsdkaddress_purification.request.v20191118.CompleteAddressRequest import CompleteAddressRequest
    class ObservedClient(AcsClient):
        # SDK is pinned to 2.16.1. Retain the exact implementation result and
        # exception; observe only its numeric HTTP status, never headers/body.
        def _implementation_of_do_action(self,*args,**kwargs):
            response=super()._implementation_of_do_action(*args,**kwargs)
            diagnostic(action,'PROVIDER_HTTP',calls=1,http_status=response[0])
            return response
    client=ObservedClient(cfg['access_key_id'],cfg['access_key_secret'],'cn-hangzhou',auto_retry=False,timeout=cfg['timeout_seconds'])
    request=(StructureAddressRequest if action=='StructureAddress' else CompleteAddressRequest)()
    request.set_endpoint('address-purification.cn-hangzhou.aliyuncs.com');request.set_protocol_type('https');request.set_method('POST')
    request.set_ServiceCode('addrp');request.set_AppKey(cfg['app_key']);request.set_Text(raw)
    return json.loads(client.do_action_with_exception(request))

def independent_proof(raw,field,value):
    from region_index import get_region_index
    index=get_region_index()
    if index is None:return None
    # Resolve customer's address only; never constrain the lookup by cloud fields.
    resolution=index.resolve(raw)
    if resolution.note and '重名' in resolution.note:return None
    from address_admin_equivalence import same
    if not same(field,getattr(resolution,field,''),value):return None
    return GeographicProof(field,value,True,'offline_region_index')

def _now_beijing():return datetime.now(timezone(timedelta(hours=8)))

def _read_usage():
    target=settings.path().with_suffix('.usage.json')
    if not target.exists():return {}
    try:data=json.loads(target.read_text())
    except (OSError,ValueError,TypeError):raise ValueError('ADDRESS_USAGE_INVALID') from None
    if not isinstance(data,dict) or not data:raise ValueError('ADDRESS_USAGE_INVALID')
    if data:
        if not isinstance(data.get('day'),str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',data['day']):raise ValueError('ADDRESS_USAGE_INVALID')
        if type(data.get('calls')) is not int or data['calls']<0:raise ValueError('ADDRESS_USAGE_INVALID')
        for key in ('actions','outcomes'):
            if key in data and (not isinstance(data[key],dict) or any(type(v) is not int or v<0 for v in data[key].values())):raise ValueError('ADDRESS_USAGE_INVALID')
            if key in data and sum(data[key].values())>data['calls']:raise ValueError('ADDRESS_USAGE_INVALID')
        if 'finished_ids' in data and not isinstance(data['finished_ids'],list):raise ValueError('ADDRESS_USAGE_INVALID')
        if 'history' in data and not isinstance(data['history'],dict):raise ValueError('ADDRESS_USAGE_INVALID')
    return data

def _usage_day(data,day):
    if data.get('day')==day:return data
    if data.get('day','')>day:raise ValueError('ADDRESS_USAGE_CLOCK_REGRESSION')
    history=dict(data.get('history',{}))
    if data.get('day'):history[data['day']]={k:v for k,v in data.items() if k!='history'}
    return dict(day=day,calls=0,actions={},outcomes={},finished_ids=[],history={k:history[k] for k in sorted(history)[-7:]})

def _usage_update(change):
    target=settings.path().with_suffix('.usage.json');target.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(str(target)+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        data=_usage_day(_read_usage(),_now_beijing().date().isoformat())
        result=change(data)
        from ai_settings import _write
        _write(target,data)
        return result

def reserve(limit,action='unknown'):
    if type(limit) is not int or not 1<=limit<=1000 or action not in ('StructureAddress','CompleteAddress','unknown'):raise ValueError('ADDRESS_LIMIT_INVALID')
    _LAST_ADMISSION.set(None)
    def change(data):
        count=data.get('calls',0)
        if count>=limit:return False
        data['calls']=count+1
        actions=data.setdefault('actions',{'unknown':count} if count else {})
        actions[action]=actions.get(action,0)+1
        _LAST_ADMISSION.set(dict(day=data['day'],action=action,id=uuid.uuid4().hex))
        return True
    return _usage_update(change)

def record_daily_outcome(action,outcome,admission=None):
    admission=admission or _LAST_ADMISSION.get()
    if action not in ('StructureAddress','CompleteAddress') or outcome not in ('success','failed','timeout') or not admission or admission['action']!=action:return
    def change(data):
        row=data if data['day']==admission['day'] else data.get('history',{}).get(admission['day'])
        if row is None:return
        ids=row.setdefault('finished_ids',[])
        if admission['id'] in ids:return
        ids.append(admission['id']);outcomes=row.setdefault('outcomes',{})
        key=action+':'+outcome;outcomes[key]=outcomes.get(key,0)+1
    return _usage_update(change)

def _safe_outcome(action,outcome,admission):
    try:return record_daily_outcome(action,outcome,admission)
    except Exception:raise TimeoutError('ADDRESS_USAGE_UNAVAILABLE') from None

def usage_snapshot(limit):
    data=_usage_day(_read_usage(),_now_beijing().date().isoformat());used=data.get('calls',0)
    next_day=(_now_beijing().date()+timedelta(days=1)).isoformat()
    return dict(day=data['day'],timezone='Asia/Shanghai',limit=limit,used=used,remaining=max(0,limit-used),
        actions=dict(data.get('actions',{'unknown':used} if used else {})),outcomes=dict(data.get('outcomes',{})),
        resets_at=next_day+' 00:00:00 Asia/Shanghai')

def _request_key(raw):
    # No cross-customer cache and no stale configuration across requests.
    try:return (raw,json.dumps(settings.read()['config'],sort_keys=True))
    except Exception:return None

from request_memo import request_memoized

@request_memoized(_request_key)
def complete(raw):
    if not isinstance(raw,str) or not 1<=len(raw)<=256 or re.search(r'1[3-9]\d{9}|手机|电话|收件|商品|口味|备选',raw):return {'state':'FAIL_COMPLETE_RESEND','reason':'UNSAFE_ADDRESS'}
    try:cfg=settings.runtime()
    except Exception:return {'state':'CONFIGURATION_FALLBACK'}
    if not cfg['enabled']:return {'state':'DISABLED','calls':0}
    if not _slots.acquire(False):return {'state':'BUSY_FALLBACK','calls':0}
    calls=0;deadline=time.monotonic()+cfg['timeout_seconds']*2;phase='CANDIDATE_EVALUATION'
    try:
        def call(action,address):
            nonlocal calls
            if time.monotonic()>=deadline:raise TimeoutError('ADDRESS_DEADLINE_EXCEEDED')
            cost_token=None
            if os.getenv('RAINBOW_PERFORMANCE_MODE','application')!='fixed_response':
                from aliyun_cost_guard import reserve as reserve_cost,estimate_for,BudgetExceeded
                service='address_structure' if action=='StructureAddress' else 'address_complete'
                from decimal import Decimal
                price=max(Decimal('0.006'),Decimal(str(cfg.get('call_cost_cny','0.006'))))
                try:cost_token=reserve_cost(service,max(estimate_for(service),price))
                except BudgetExceeded as error:
                    reason={'ALIYUN_CASH_BUDGET_EXHAUSTED':'ADDRESS_CASH_BUDGET_EXHAUSTED',
                            'ALIYUN_MONTHLY_BUDGET_EXHAUSTED':'ADDRESS_MONTHLY_BUDGET_EXHAUSTED',
                            'ALIYUN_COST_CIRCUIT_OPEN':'ADDRESS_SERVICE_CIRCUIT_OPEN'}.get(str(error),'ADDRESS_COST_GUARD_UNAVAILABLE')
                    raise TimeoutError(reason) from None
            try:
                if not reserve(cfg['daily_limit'],action):raise TimeoutError('ADDRESS_BUDGET_EXHAUSTED')
            except Exception as error:
                if cost_token:
                    from aliyun_cost_guard import cancel_before_send
                    cancel_before_send(cost_token)
                if isinstance(error,TimeoutError):raise
                raise TimeoutError('ADDRESS_USAGE_UNAVAILABLE') from None
            admission=_LAST_ADMISSION.get()
            calls+=1
            from order_trace import stage
            started=time.monotonic();failed=False
            try:
                with stage('ADDRESS_'+action.upper()):return transport(action,address,cfg)
            except Exception as error:
                failed=True
                diagnostic(action,'PROVIDER_TRANSPORT_FAILED',error,calls=calls,final_state='FAIL_COMPLETE_RESEND')
                from execution_safety import technical_failure
                reason=provider_failure_reason(error)
                technical_failure(reason)
                _safe_outcome(action,'timeout' if isinstance(error,TimeoutError) or type(error).__name__ in ('ReadTimeout','ConnectTimeout','ReadTimeoutError') else 'failed',admission)
                raise TimeoutError(reason) from None
            finally:
                from performance_metrics import record_sample,mode
                provenance='fixed_response' if mode()=='fixed_response' else 'cloud_live'
                record_sample(provenance,'ADDRESS_HTTP',(time.monotonic()-started)*1000,error=failed,calls=1,cost_cny=cfg['call_cost_cny'],cost_kind='simulated' if provenance=='fixed_response' else 'estimated',provider='AliyunAddress')
                if cost_token and not failed:
                    try:
                        from aliyun_cost_guard import settle
                        settle(cost_token,price)
                    except Exception:pass
                if not failed:_safe_outcome(action,'success',admission)
                if cost_token:
                    try:
                        from aliyun_cost_guard import record_outcome
                        record_outcome(service,not failed)
                    except Exception:pass
        provider=AliyunAddressCandidate(lambda a:call('StructureAddress',a),lambda a:call('CompleteAddress',a),independent_proof,cloud_primary=True)
        result=provider.evaluate(raw)
        # Result reasons are adapter-owned constants, never provider text.
        if result['state']=='FAIL_COMPLETE_RESEND':
            reason=result.get('reason')
            allowed={'ADDRESS_MISSING','STRUCTURE_UNAVAILABLE','STRUCTURE_EMPTY','UNSUPPORTED_DETAIL_GUESS','COMPLETE_UNAVAILABLE','CLOUD_ADMIN_CONFLICT','INDEPENDENT_PROOF_INVALID','INDEPENDENT_PROOF_NOT_UNIQUE','NO_SAFE_COMPLETION'}
            diagnostic(phase,reason if reason in allowed else 'CANDIDATE_REJECTED',calls=calls,final_state=result['state'])
        if result['state'] in ('SAFE','PROPOSE_ADMIN_COMPLETION','CONFIRM_ADDRESS'):
            from cloud_address_trust import safety,remember
            phase='FINAL_CONTACT_VALIDATION'
            if not safety(raw,result['address'],result.get('cloud_fields') or {}):
                return {'state':'FAIL_COMPLETE_RESEND','reason':'SOURCE_ADDRESS_CONFLICT','calls':calls}
            if result['state']!='CONFIRM_ADDRESS':remember(result['address'])
        return dict(result,calls=calls)
    except TimeoutError as error:
        reason=str(error)
        if reason not in ('ADDRESS_BUDGET_EXHAUSTED','ADDRESS_MONTHLY_BUDGET_EXHAUSTED','ADDRESS_DEADLINE_EXCEEDED','ADDRESS_CASH_BUDGET_EXHAUSTED','ADDRESS_SERVICE_CIRCUIT_OPEN','ADDRESS_COST_GUARD_UNAVAILABLE','ADDRESS_USAGE_UNAVAILABLE','ADDRESS_ACCOUNT_BALANCE_INSUFFICIENT','ADDRESS_PROVIDER_RATE_LIMITED'):
            reason='ADDRESS_SERVICE_FAILED'
        from execution_safety import technical_failure
        diagnostic(phase,reason,calls=calls,final_state='FAIL_COMPLETE_RESEND')
        technical_failure(reason)
        return {'state':'FAIL_COMPLETE_RESEND','reason':reason,'calls':calls}
    except Exception as error:
        diagnostic(phase,'LOCAL_VALIDATION_OR_ADAPTER_EXCEPTION',error,calls=calls,final_state='FAIL_COMPLETE_RESEND')
        from execution_safety import technical_failure
        from cloud_address_trust import AddressTrustStorageError
        reason='ADDRESS_TRUST_STORAGE_UNAVAILABLE' if isinstance(error,AddressTrustStorageError) else 'ADDRESS_SERVICE_FAILED'
        technical_failure(reason)
        return {'state':'FAIL_COMPLETE_RESEND','reason':reason,'calls':calls}
    finally:_slots.release()
