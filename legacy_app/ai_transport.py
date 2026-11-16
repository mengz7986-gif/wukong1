"""Provider transport: no raw upstream errors or customer text in logs."""
import os
import time
import threading
from urllib.parse import urlsplit
import requests
import uuid
from ai_settings import AIConfigError, validate_url, record_call
from ai_diagnostics import CURRENT, request, event


class AIRequestError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


ERROR_LABELS = {'AUTH_ERROR': 'API Key 无效或认证失败', 'CREDIT_ERROR': 'AI凭证额度不足', 'BUDGET_ERROR': '本地费用保护已停止新增付费调用', 'RATE_LIMIT': 'AI调用频率受限', 'URL_ERROR': 'Base URL 错误或主机未获服务端允许', 'MODEL_ERROR': 'Model 不存在或请求配置无效', 'TIMEOUT': '连接超时', 'NETWORK_ERROR': '网络异常', 'PROVIDER_ERROR': 'Provider 暂时异常', 'INVALID_RESPONSE': 'Provider 返回格式无效', 'KEY_MISSING': 'API Key 未配置', 'AI_DISABLED': 'AI 功能已关闭'}

_connections = threading.local()


def _response_shape(response):
    """Return parsed body plus privacy-safe structural facts only."""
    try:
        body = response.json()
    except (ValueError, TypeError, AttributeError):
        return None, {'response_json_object':False}
    shape = {'response_json_object':isinstance(body,dict)}
    if not isinstance(body,dict):
        return body,shape
    choices=body.get('choices')
    shape['response_has_choices']=isinstance(choices,list) and bool(choices)
    first=choices[0] if shape['response_has_choices'] and isinstance(choices[0],dict) else {}
    message=first.get('message') if isinstance(first,dict) else None
    shape['response_has_message']=isinstance(message,dict)
    content=message.get('content') if isinstance(message,dict) else None
    reasoning=message.get('reasoning_content') if isinstance(message,dict) else None
    shape['response_has_content']=isinstance(content,str) and bool(content.strip())
    shape['response_has_reasoning']=isinstance(reasoning,str) and bool(reasoning.strip())
    shape['response_content_chars']=len(content) if isinstance(content,str) else 0
    shape['response_reasoning_chars']=len(reasoning) if isinstance(reasoning,str) else 0
    shape['response_has_usage']=isinstance(body.get('usage'),dict)
    finish=first.get('finish_reason') if isinstance(first,dict) else None
    shape['response_finish_reason']=finish if finish in ('stop','length','content_filter','tool_calls') else 'unknown'
    return body,shape

def provider_post(url, **kwargs):
    """Thread-owned keepalive pool; never retry a request or retain credentials."""
    from execution_safety import ensure_running
    ensure_running()
    session = getattr(_connections, 'session', None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
        session.mount('https://', adapter)
        _connections.session = session
    session.cookies.clear()
    try:
        return session.post(url, **kwargs)
    finally:
        session.cookies.clear()


async def run_provider_worker(function, *args):
    """Separate executor scheduling from provider latency without message text."""
    import asyncio
    from order_trace import mark
    from ai_work_budget import pool,remaining
    queued = time.monotonic()
    def run():
        mark('AI_WORKER_QUEUE', (time.monotonic()-queued)*1000)
        return function(*args)
    permits=pool();left=remaining()
    try:
        if left is None:await permits.acquire()
        else:await asyncio.wait_for(permits.acquire(),timeout=left)
    except asyncio.TimeoutError:
        from execution_safety import technical_failure
        technical_failure('TIMEOUT')
        raise AIRequestError('TIMEOUT') from None
    task=None
    try:
        left=remaining()
        if left is not None and left<=0:
            from execution_safety import technical_failure
            technical_failure('TIMEOUT')
            raise AIRequestError('TIMEOUT')
        queued=time.monotonic()
        task=asyncio.create_task(asyncio.to_thread(run))
        # Do not cancel the wrapper on expiry: its underlying thread may
        # still be executing a request. Keep its permit until actual finish.
        def finished(completed):
            permits.release()
            if not completed.cancelled():completed.exception()
        task.add_done_callback(finished)
        done,_=await asyncio.wait((task,),timeout=left)
        if not done:
            from execution_safety import technical_failure
            technical_failure('TIMEOUT')
            raise AIRequestError('TIMEOUT')
        return task.result()
    finally:
        if task is None:permits.release()


def completion(settings, messages, kind, actor='bot', json_mode=True, validator=None):
    current = CURRENT.get()
    if not current or current['kind'] != kind:
        text = messages[-1].get('content','') if messages else ''
        with request(kind, text, 'SKIPPED'):
            return _completion(settings, messages, kind, actor, json_mode, validator)
    return _completion(settings, messages, kind, actor, json_mode, validator)


def _completion(settings, messages, kind, actor, json_mode, validator):
    from network_safety import retry_available,backoff
    for attempt in ((1,) if kind == 'FAILURE_ANALYSIS' else (1,2)):
        try:
            value=_completion_attempt(settings,messages,kind,actor,json_mode,validator,attempt)
            if attempt==2 and validator is not None:
                event('FINAL_RESULT','AWAITING_VALIDATION')
            return value
        except AIRequestError as error:
            details=getattr(error,'network_details',None)
            if kind != 'FAILURE_ANALYSIS' and attempt==1 and details and retry_available(settings,kind,details):
                backoff()
                continue
            if details:
                from execution_safety import technical_failure
                technical_failure(error.category)
            raise


def _completion_attempt(settings, messages, kind, actor, json_mode, validator, attempt):
    started = time.monotonic()
    category = None
    attempt_id = uuid.uuid4().hex
    http_elapsed=None
    event('AI_REQUEST', 'PENDING', ai_called=True, settings=settings)
    cost_token=None;actual_price=None
    try:
        if settings.get('provider') not in ('DeepSeek','OpenRouter'):
            raise AIRequestError('MODEL_ERROR')
        if not settings.get('api_key'):
            raise AIRequestError('KEY_MISSING')
        try:
            base = validate_url(settings['base_url'])
        except AIConfigError:
            raise AIRequestError('URL_ERROR') from None
        allowed = {x.strip().lower() for x in os.getenv(
            'AI_ALLOWED_HOSTS',
            'api.deepseek.com,openrouter.ai',
        ).split(',') if x.strip()}
        if urlsplit(base).hostname.lower() not in allowed:
            raise AIRequestError('URL_ERROR')
        payload = {'model': settings['model'], 'temperature': 0, 'messages': messages}
        if kind == 'FAILURE_ANALYSIS':
            payload['max_tokens'] = 1536
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}
        else:
            # Reasoning-capable endpoints can spend a tiny allowance before
            # emitting visible content. Eight tokens produced HTTP 200 with
            # an empty final content on the official DeepSeek probe.
            # Connectivity probes need enough room for reasoning-capable
            # models to emit the requested visible token. Business calls keep
            # the tighter allowance unless their caller uses JSON mode.
            payload['max_tokens'] = 128 if kind == 'TEST_CONNECTION' else 64
        from order_trace import stage
        with stage('PROVIDER_HTTP'):
            from ai_work_budget import http_timeout
            try:timeout=http_timeout(settings['timeout'])
            except TimeoutError:raise AIRequestError('TIMEOUT') from None
            http_started=time.monotonic()
            try:
                response = provider_post(base + '/chat/completions', headers={'Authorization': 'Bearer ' + settings['api_key'], 'Content-Type': 'application/json'}, json=payload, timeout=timeout, allow_redirects=False)
            finally:
                http_elapsed=(time.monotonic()-http_started)*1000
        status = getattr(response, 'status_code', 200)
        retry_after = None
        if status == 429:
            try:
                header = response.headers.get('Retry-After')
                try:
                    retry_after = float(header)
                except (TypeError, ValueError):
                    from email.utils import parsedate_to_datetime
                    from datetime import datetime, timezone
                    retry_after = max(0, (parsedate_to_datetime(header)-datetime.now(timezone.utc)).total_seconds())
            except Exception:
                pass
        response_body=None;response_shape={}
        if status == 200:
            response_body,response_shape=_response_shape(response)
        event('PROVIDER_RESULT', 'SUCCESS' if status == 200 else 'FAILED',
              http_status=status, elapsed_ms=round((time.monotonic()-started)*1000),
              retry_after=retry_after, **response_shape)
        if status in (401, 403):
            raise AIRequestError('AUTH_ERROR')
        if status == 402:
            raise AIRequestError('CREDIT_ERROR')
        if status == 429:
            raise AIRequestError('RATE_LIMIT')
        if status in (400, 404, 422):
            raise AIRequestError('MODEL_ERROR')
        if status != 200:
            raise AIRequestError('PROVIDER_ERROR')
        try:
            content = response_body['choices'][0]['message']['content']
            if not isinstance(content, str) or not content.strip():
                raise ValueError()
            if validator is not None:
                with stage('AI_NORMALIZATION'):
                    value = validator(content)
                event('NORMALIZATION_RESULT', 'SUCCESS', candidate=value)
                return value
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            event('NORMALIZATION_RESULT', 'REJECTED', ['INVALID_RESPONSE'])
            raise AIRequestError('INVALID_RESPONSE') from None
        event('NORMALIZATION_RESULT', 'SKIPPED')
        event('DETERMINISTIC_VALIDATION', 'SKIPPED')
        event('FINAL_RESULT', 'SUCCESS')
        return content
    except requests.RequestException as error:
        from network_safety import classify,observe
        details=classify(error)
        category='TIMEOUT' if isinstance(error,requests.Timeout) else 'NETWORK_ERROR'
        observe(details,kind,attempt,(time.monotonic()-started)*1000)
        failure=AIRequestError(category);failure.network_details=details
        raise failure from None
    except AIRequestError as error:
        category = error.category
        from execution_safety import technical_failure
        technical_failure(category)
        raise
    finally:
        if http_elapsed is not None:
            try:
                from performance_metrics import record_sample,mode
                from decimal import Decimal
                provenance='fixed_response' if mode()=='fixed_response' else 'cloud_live'
                price=None
                if 'response' in locals() and getattr(response,'status_code',0)==200:
                    usage=response.json().get('usage',{})
                    input_rate=os.getenv('RAINBOW_AI_INPUT_CNY_PER_MILLION')
                    output_rate=os.getenv('RAINBOW_AI_OUTPUT_CNY_PER_MILLION')
                    if input_rate and output_rate and all(type(usage.get(k)) is int and usage[k]>=0 for k in ('prompt_tokens','completion_tokens')):
                        price=(Decimal(input_rate)*usage['prompt_tokens']+Decimal(output_rate)*usage['completion_tokens'])/Decimal(1000000)
                        actual_price=price
                metric={'ORDER_PARSE':'ORDER_MODEL_HTTP','CONTACT_PARSE':'CONTACT_MODEL_HTTP','ORDER_FLAVOR_CANDIDATE':'FLAVOR_MODEL_HTTP'}.get(kind,'PROVIDER_HTTP')
                record_sample(provenance,metric,http_elapsed,calls=1,error=category is not None,cost_cny=price,cost_kind='simulated' if provenance=='fixed_response' else 'estimated',provider=settings.get('provider'))
                from ai_usage_ledger import record_attempt
                diagnostic=CURRENT.get() or {}
                recorded_status=getattr(response,'status_code',None) if 'response' in locals() else None
                record_attempt(
                    settings=settings, kind=kind,
                    response=response if 'response' in locals() else None,
                    provenance=provenance,
                    logical_request_id=diagnostic.get('request_id'),
                    trace_id=diagnostic.get('trace_id'),
                    attempt_id=attempt_id,
                    http_status=recorded_status,
                    outcome='SUCCESS' if category is None and recorded_status==200 else (category or 'OTHER_ERROR'),
                )
            except Exception:pass
        if category:
            if category != 'INVALID_RESPONSE':
                event('PROVIDER_RESULT', 'FAILED', [category], elapsed_ms=round((time.monotonic()-started)*1000))
                event('NORMALIZATION_RESULT', 'SKIPPED')
            event('DETERMINISTIC_VALIDATION', 'SKIPPED')
            event('FINAL_RESULT', 'FAILED' if category != 'INVALID_RESPONSE' else 'REJECTED', [category])
        try:
            record_call(kind, settings, category is None, time.monotonic()-started, category, actor)
        except (OSError, AIConfigError):
            pass


def test_connection(settings, actor):
    started = time.monotonic()
    try:
        completion(settings, [{'role': 'user', 'content': '只回复一个可见字符：好'}], 'TEST_CONNECTION', actor, json_mode=False)
        return {'success': True, 'message': 'AI连接成功', 'provider': settings['provider'], 'model': settings['model'], 'elapsed_ms': round((time.monotonic()-started)*1000)}
    except AIRequestError as error:
        return {'success': False, 'message': ERROR_LABELS.get(error.category, 'Provider 错误'), 'error_type': error.category}
