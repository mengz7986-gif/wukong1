"""Explicit administrator-triggered, read-only DeepSeek balance lookup."""
import datetime as dt
import requests


def fetch(timeout=5):
    from ai_settings import get_settings
    settings=get_settings()
    if settings.get('provider')!='DeepSeek' or settings.get('base_url')!='https://api.deepseek.com':
        return {'status':'UNAVAILABLE','reason':'当前生效配置不是DeepSeek官方API'}
    key=str(settings.get('api_key') or '')
    if not key:return {'status':'UNAVAILABLE','reason':'当前生效Key未配置'}
    try:
        response=requests.get(
            'https://api.deepseek.com/user/balance',
            headers={'Authorization':'Bearer '+key,'Accept':'application/json'},
            timeout=max(1,min(int(timeout),8)),allow_redirects=False)
    except requests.Timeout:
        return {'status':'FAILED','error_class':'TIMEOUT'}
    except requests.RequestException:
        return {'status':'FAILED','error_class':'NETWORK_ERROR'}
    if response.status_code!=200:
        return {'status':'FAILED','error_class':'HTTP_STATUS','http_status':response.status_code}
    try:payload=response.json()
    except ValueError:return {'status':'FAILED','error_class':'INVALID_RESPONSE'}
    rows=[]
    for row in payload.get('balance_infos') or []:
        if not isinstance(row,dict):continue
        rows.append({key:row.get(key) for key in
                     ('currency','total_balance','granted_balance','topped_up_balance')})
    return {'status':'SUCCESS','checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
            'is_available':bool(payload.get('is_available')),'balance_infos':rows,
            'request_count':1,'read_only':True,'auto_recharge':False}
