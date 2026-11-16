"""Independent encrypted Address Purification settings, disabled by default."""
import os,json,fcntl
from pathlib import Path
from datetime import datetime,timezone
from ai_settings import _cipher,_write

SECRETS=('access_key_id','access_key_secret','app_key')
DEFAULTS=dict(enabled=False,timeout_seconds=2,daily_limit=10,call_cost_cny='0.006')

def path():return Path(os.getenv('RAINBOW_ALIYUN_ADDRESS_PATH') or Path(__file__).with_name('aliyun_address_settings.json'))

def read():
    if not path().exists():return {'config':dict(DEFAULTS),'audit':[]}
    value=json.loads(path().read_text())
    if not isinstance(value,dict) or not isinstance(value.get('config'),dict):raise ValueError('ADDRESS_CONFIG_INVALID')
    if set(value['config'])-set(DEFAULTS)-{k+'_ciphertext' for k in SECRETS}:raise ValueError('ADDRESS_CONFIG_INVALID')
    return value

def validate(cfg):
    from decimal import Decimal
    if type(cfg.get('enabled')) is not bool:raise ValueError('地址开关必须为布尔值')
    for k,high in [('timeout_seconds',3),('daily_limit',1000)]:
        if type(cfg.get(k)) is not int or not 1<=cfg[k]<=high:raise ValueError('地址参数超出安全范围')
    price=Decimal(str(cfg.get('call_cost_cny')))
    if not price.is_finite() or not 0<=price<=1:raise ValueError('地址费用估算无效')

def runtime():
    cfg=dict(read()['config']);validate(cfg)
    for field in SECRETS:
        encrypted=cfg.pop(field+'_ciphertext','')
        cfg[field]=_cipher().decrypt(encrypted.encode()).decode() if encrypted else ''
    return cfg

def public():
    try:
        cfg=dict(read()['config']);validate(cfg)
        for field in SECRETS:cfg[field+'_configured']=bool(cfg.pop(field+'_ciphertext',''))
        cfg['configuration_error']=False
    except Exception:
        cfg=dict(DEFAULTS,configuration_error=True,**{k+'_configured':False for k in SECRETS})
    return dict(cfg,provider='AliyunAddress',region='cn-hangzhou',endpoint_fixed=True)

def save(body,actor):
    if not isinstance(body,dict) or set(body)-set(DEFAULTS)-set(SECRETS):raise ValueError('地址配置字段无效')
    target=path();target.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(str(target)+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        stored=read();cfg=dict(stored['config'])
        cfg.update({k:body[k] for k in DEFAULTS if k in body});validate(cfg)
        for field in SECRETS:
            if field in body:
                value=body[field]
                if not isinstance(value,str) or len(value)>256 or any(c.isspace() for c in value):raise ValueError('地址凭证格式无效')
                if value:cfg[field+'_ciphertext']=_cipher().encrypt(value.encode()).decode()
        if cfg['enabled']:
            if not all(cfg.get(k+'_ciphertext') for k in SECRETS):raise ValueError('启用前须配置独立地址凭证')
            for field in SECRETS:_cipher().decrypt(cfg[field+'_ciphertext'].encode())
        stored['config']=cfg
        stored['audit']=(stored.get('audit',[])+[dict(actor=str(actor)[:100],action='SAVE_ADDRESS_CONFIG',at=datetime.now(timezone.utc).isoformat())])[-100:]
        _write(target,stored)
    return public()
