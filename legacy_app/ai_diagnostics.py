"""Bounded, allowlisted AI telemetry. Never stores prompts, contacts or keys."""
import contextvars
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from ai_settings import _read, _write
import fcntl
from datetime import datetime, timezone, timedelta

CURRENT = contextvars.ContextVar('ai_diagnostic_request', default=None)
STAGES = {'LOCAL_RESULT', 'AI_REQUEST', 'PROVIDER_RESULT', 'NORMALIZATION_RESULT',
          'DETERMINISTIC_VALIDATION', 'FINAL_RESULT'}
CODES = {'INVALID_RESPONSE', 'CATEGORY_NOT_FOUND', 'PRODUCT_NOT_FOUND', 'ALIAS_CONFLICT',
         'SOURCE_EVIDENCE_FAILED', 'QUANTITY_INVALID', 'CONTACT_INVALID',
         'CATALOG_VALIDATION_FAILED', 'CONFIDENCE_REJECTED', 'BUSINESS_REJECTION',
         'UNKNOWN_VALIDATION', 'AUTH_ERROR', 'CREDIT_ERROR', 'RATE_LIMIT', 'URL_ERROR',
         'MODEL_ERROR', 'TIMEOUT', 'NETWORK_ERROR', 'PROVIDER_ERROR', 'KEY_MISSING',
         'AI_DISABLED', 'INTERNAL_ERROR'}
OUTCOMES = {'SUCCESS', 'REJECTED', 'FAILED', 'PENDING', 'AWAITING_VALIDATION',
            'LOCAL_ONLY', 'LOCAL_FALLBACK', 'NOT_CALLED', 'SKIPPED'}

def _path():
    override = os.getenv('AI_DIAGNOSTICS_PATH')
    if override:
        return Path(override)
    configured = os.getenv('AI_SETTINGS_PATH')
    return Path(configured).with_suffix('.diagnostics.json') if configured else Path(__file__).with_name('ai_diagnostics.json')

def _now():
    return datetime.now(timezone.utc).isoformat()

def _fingerprint(value):
    return hashlib.sha256(str(value or '').encode('utf-8')).hexdigest()

def candidate_summary(value):
    """No customer strings, including strings embedded in item/name fields."""
    if not isinstance(value, dict):
        return {'object': False}
    items = value.get('items') or []
    if not isinstance(items, list):
        items = []
    summary = []
    for item in items[:50]:
        if not isinstance(item, dict):
            continue
        q = item.get('quantity')
        summary.append({'category_fingerprint': _fingerprint(item.get('category')),
                        'product_fingerprint': _fingerprint(item.get('flavor')),
                        'source_fingerprint': _fingerprint(item.get('source_text')),
                        'quantity': q if type(q) is int and 0 < q <= 100000 else None})
    return {'object': True, 'items': summary, 'item_count': len(items),
            'contact_present': {f: bool(value.get(f)) for f in ('recipient','phone','address')}}

def _save(record, stage=None, outcome=None, codes=(), **fields):
    """Telemetry failure must never alter an order or AI result."""
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path)+'.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = _read(path)
            rows = data.get('calls', [])
            cutoff = (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
            rows = [r for r in rows if isinstance(r,dict) and r.get('at','') >= cutoff]
            row = next((r for r in rows if r.get('request_id') == record['request_id']), None)
            if row is None:
                row = dict(record, stages=[])
                rows.append(row)
            safe_codes = list(dict.fromkeys(c if c in CODES else 'UNKNOWN_VALIDATION' for c in codes))
            if stage in STAGES:
                event = {'stage':stage, 'at':_now(), 'outcome':outcome if outcome in OUTCOMES else 'PENDING', 'codes':safe_codes}
                row['stages'] = (row.get('stages', [])+[event])[-16:]
                row[stage.lower()] = event['outcome']
                if stage == 'PROVIDER_RESULT':
                    row['provider_codes'] = safe_codes
                if stage == 'DETERMINISTIC_VALIDATION':
                    row['validation_codes'] = safe_codes
                if stage == 'NORMALIZATION_RESULT' and safe_codes:
                    row['normalization_codes'] = safe_codes
                if stage == 'FINAL_RESULT':
                    row['final_codes'] = safe_codes
            # Explicit types/allowlist; no arbitrary exception or provider body.
            for key in ('ai_called','local_only','contact_fallback'):
                if key in fields:
                    row[key] = bool(fields[key])
            # Response-shape telemetry is deliberately content-free.  It lets
            # operators distinguish an empty visible answer from malformed
            # JSON without retaining provider text or reasoning.
            for key in ('response_json_object','response_has_choices',
                        'response_has_message','response_has_content',
                        'response_has_reasoning','response_has_usage'):
                if key in fields:
                    row[key] = bool(fields[key])
            for key in ('elapsed_ms','http_status'):
                if type(fields.get(key)) is int:
                    row[key] = max(0, fields[key])
            for key in ('response_content_chars','response_reasoning_chars'):
                if type(fields.get(key)) is int:
                    row[key] = max(0,min(fields[key],1000000))
            finish_reason = fields.get('response_finish_reason')
            if finish_reason in ('stop','length','content_filter','tool_calls','unknown'):
                row['response_finish_reason'] = finish_reason
            retry = fields.get('retry_after')
            if type(retry) in (int,float) and 0 <= retry <= 86400:
                row['retry_after'] = retry
            settings = fields.get('settings')
            if isinstance(settings, dict):
                row['provider'] = settings.get('provider') if settings.get('provider') in ('DeepSeek','OpenRouter','AliyunBailian') else 'Other'
                # Model identifiers only; never arbitrary text/config credentials.
                model = str(settings.get('model') or '')
                row['model'] = model if re.fullmatch(r'[A-Za-z0-9_./:-]{1,120}',model) and not model.startswith(('sk-','Bearer')) else 'redacted'
            if 'candidate' in fields:
                row['candidate'] = candidate_summary(fields['candidate'])
            _write(path, {'version':1, 'calls':rows[-200:]})
    except Exception:
        pass

@contextmanager
def request(kind, text='', local_outcome='PENDING'):
    record = {'request_id':uuid.uuid4().hex, 'kind':kind if kind in ('ORDER_PARSE','CONTACT_PARSE','TEST_CONNECTION','ORDER_IMAGE_PARSE','FAILURE_ANALYSIS') else 'OTHER',
              'at':_now(), 'text_fingerprint':_fingerprint(text), 'ai_called':False, 'local_only':False}
    from order_trace import CURRENT as ORDER_TRACE
    order_trace = ORDER_TRACE.get()
    if order_trace:
        record['trace_id'] = order_trace['trace_id']
    token = CURRENT.set(record)
    _save(record, 'LOCAL_RESULT', local_outcome)
    try:
        yield record['request_id']
    finally:
        CURRENT.reset(token)

def event(stage, outcome, codes=(), **fields):
    record = CURRENT.get()
    if record:
        # In-memory terminal facts let the authorized handler retain separate
        # private review evidence. The public telemetry remains allowlisted.
        if stage == 'FINAL_RESULT':
            record['_terminal_outcome'] = outcome
            record['_terminal_codes'] = list(codes)
        _save(record, stage, outcome, codes, **fields)

def reject(codes):
    event('DETERMINISTIC_VALIDATION','REJECTED',codes)
    event('FINAL_RESULT','REJECTED',codes)

def history(limit=20):
    try:
        rows = _read(_path()).get('calls',[])
        cutoff = (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
        rows = [r for r in rows if isinstance(r,dict) and r.get('at','') >= cutoff]
        return list(reversed(rows[-max(1,min(int(limit),200)):]))
    except Exception:
        return []


def associate_order(trace_id, order_numbers):
    """Attach safe business references after a successful commit."""
    if not isinstance(trace_id,str) or not re.fullmatch(r'[a-f0-9]{32}',trace_id):
        raise ValueError('追踪编号无效')
    if isinstance(order_numbers,str):
        order_numbers=[order_numbers]
    if not isinstance(order_numbers,list) or not 1<=len(order_numbers)<=100:
        raise ValueError('订单编号无效')
    numbers=[]
    for value in order_numbers:
        value=str(value or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value):
            raise ValueError('订单编号无效')
        numbers.append(value)
    path=_path()
    fd=os.open(str(path)+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        data=_read(path);matched=0
        for row in data.get('calls',[]):
            if row.get('trace_id')==trace_id:
                row['order_numbers']=numbers;matched+=1
        if matched:_write(path,data)
    return {'matched':matched,'order_numbers':numbers}
