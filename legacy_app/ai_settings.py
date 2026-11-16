"""Single runtime AI configuration source. No customer text in this store."""
import fcntl
import ipaddress
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
import config


class AIConfigError(RuntimeError):
    pass


def _path():
    return Path(os.getenv('AI_SETTINGS_PATH') or Path(__file__).with_name('ai_settings.json'))


def _cipher():
    try:
        return Fernet(os.environ['AI_SETTINGS_MASTER_KEY'].encode('ascii'))
    except (KeyError, ValueError, UnicodeError):
        raise AIConfigError('AI 加密主密钥未配置或格式无效') from None


def encryption_ready():
    try:
        _cipher()
        return True
    except AIConfigError:
        return False


def _read(path):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise AIConfigError('AI 配置文件损坏或无法读取；不会静默切换旧 Key') from None


@contextmanager
def _locked():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix='.ai-settings-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_url(value):
    value = str(value or '').strip().rstrip('/')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or port not in (None, 443):
            raise ValueError()
        if parsed.hostname.lower() == 'localhost' or parsed.hostname.endswith('.local'):
            raise ValueError()
        if parsed.hostname.lower().endswith(('.aliyuncs.com','.aliyun.com')):
            raise ValueError()
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValueError()
    except ValueError:
        raise AIConfigError('Base URL 必须是无凭据、无查询参数的公网 HTTPS 地址（443端口）') from None
    return value


def retired():
    return _read(_path()).get('retired') is True


def get_settings(fallback=None):
    if retired():
        return dict(enabled=False, order_enabled=False, contact_enabled=False,
                    provider='Local', base_url='', model='', timeout=3,
                    api_key='', source='本地识别')
    enabled = config.DEEPSEEK_ENABLED
    result = {
        'enabled': enabled, 'order_enabled': enabled, 'contact_enabled': enabled,
        'provider': os.getenv('AI_PROVIDER', 'DeepSeek'), 'base_url': config.DEEPSEEK_BASE_URL,
        'model': config.DEEPSEEK_MODEL, 'timeout': config.DEEPSEEK_TIMEOUT,
        'api_key': config.DEEPSEEK_API_KEY, 'source': '.env fallback',
    }
    result.update(fallback or {})
    if result.get('provider') not in ('DeepSeek','OpenRouter'):
        raise AIConfigError('旧服务商已移除，请重新配置受支持的AI服务')
    data = _read(_path())
    saved = data.get('config')
    if saved is not None:
        required = {'enabled', 'order_enabled', 'contact_enabled', 'provider', 'base_url', 'model', 'timeout', 'key_ciphertext'}
        if not isinstance(saved, dict) or set(saved) != required or saved.get('provider') not in ('DeepSeek', 'OpenRouter') or any(type(saved.get(k)) is not bool for k in ('enabled', 'order_enabled', 'contact_enabled')):
            raise AIConfigError('AI 后台配置结构无效；不会静默切换旧配置')
        result.update(saved)
        result['source'] = '后台配置'
        # A saved backend configuration is a complete snapshot, not a mixture
        # with .env credentials. Empty ciphertext means deliberately no key.
        token = result.pop('key_ciphertext', '')
        try:
            result['api_key'] = _cipher().decrypt(token.encode('ascii')).decode('utf-8') if token else ''
        except (InvalidToken, ValueError, UnicodeError):
            raise AIConfigError('AI 密钥无法解密，请检查服务端主密钥') from None
    return result


def public_status():
    from ai_diagnostics import history
    data = _read(_path())
    try:
        settings = get_settings()
        status = {k: v for k, v in settings.items() if k != 'api_key'}
        status['key_configured'] = bool(settings['api_key'])
    except AIConfigError as error:
        status = dict(data.get('config') or {})
        status['key_configured'] = bool(status.pop('key_ciphertext', ''))
        status['source'] = '后台配置'
        status['configuration_error'] = str(error)
        if status.get('provider') not in ('DeepSeek','OpenRouter'):
            status.update(provider='未配置',model='',base_url='',enabled=False,order_enabled=False,contact_enabled=False)
    if data.get('retired') is True:
        return dict(status, secret_status='不需要', encryption_ready=encryption_ready(),
                    recent_calls={}, recent_test=None, diagnostic_calls=[])
    status.update(secret_status='不需要', encryption_ready=encryption_ready(),
                  recent_calls=data.get('recent_calls', {}), recent_test=data.get('recent_calls', {}).get('TEST_CONNECTION'))
    status['diagnostic_calls'] = history(20)
    return status


def _audit(data, actor, action):
    history = data.get('audit', [])
    history.append({'actor': str(actor)[:100], 'action': action, 'at': datetime.now(timezone.utc).isoformat()})
    data['audit'] = history[-100:]


def save_settings(values, actor):
    if retired():
        raise AIConfigError('外部模型功能已移除，不能重新启用')
    allowed = {'enabled', 'order_enabled', 'contact_enabled', 'provider', 'base_url', 'model', 'timeout', 'api_key'}
    if set(values) - allowed:
        raise AIConfigError('AI 配置包含不支持的字段')
    with _locked() as path:
        data = _read(path)
        try:
            current = get_settings()
        except AIConfigError:
            # Explicit admin replacement of a retired provider starts empty;
            # its old ciphertext must never become a different vendor's key.
            saved=data.get('config') or {}
            if saved.get('provider')!='AliyunBailian' or values.get('provider') not in ('DeepSeek','OpenRouter'):
                raise
            current=dict(enabled=False,order_enabled=False,contact_enabled=False,
                         provider=values['provider'],base_url=config.DEEPSEEK_BASE_URL,
                         model=config.DEEPSEEK_MODEL,timeout=config.DEEPSEEK_TIMEOUT,api_key='')
        current.update(values)
        if current['provider'] not in ('DeepSeek', 'OpenRouter'):
            raise AIConfigError('当前支持 DeepSeek / OpenRouter')
        for flag in ('enabled', 'order_enabled', 'contact_enabled'):
            if not isinstance(current[flag], bool):
                raise AIConfigError('AI 开关必须是布尔值')
        current['base_url'] = validate_url(current['base_url'])
        if not isinstance(current['model'], str) or not current['model'].strip() or len(current['model']) > 120:
            raise AIConfigError('Model 无效')
        if type(current['timeout']) is not int or not 3 <= current['timeout'] <= 60:
            raise AIConfigError('timeout 必须在 3–60 秒之间')
        key = current.pop('api_key')
        if not isinstance(key, str) or len(key) > 4096 or any(c in key for c in '\r\n'):
            raise AIConfigError('API Key 格式无效')
        current.pop('source', None)
        current['key_ciphertext'] = _cipher().encrypt(key.encode('utf-8')).decode('ascii') if key else ''
        data['config'] = current
        _audit(data, actor, 'SAVE_CONFIG')
        _write(path, data)
    return public_status()


def reveal_key(actor, action='REVEAL'):
    if action not in ('REVEAL', 'COPY'):
        raise AIConfigError('操作无效')
    with _locked() as path:
        data = _read(path)
        key = get_settings()['api_key']
        _audit(data, actor, action)
        _write(path, data)
    return key


def record_call(kind, settings, success, elapsed, error_type=None, actor='bot'):
    # Explicit allowlist only; NEVER persist prompt, responses or exceptions.
    with _locked() as path:
        data = _read(path)
        data.setdefault('recent_calls', {})[kind] = {
            'at': datetime.now(timezone.utc).isoformat(), 'kind': kind,
            'provider': settings.get('provider'), 'model': settings.get('model'),
            'success': bool(success), 'elapsed_ms': round(elapsed * 1000),
            'error_type': error_type,
        }
        if kind == 'TEST_CONNECTION':
            _audit(data, actor, kind)
        _write(path, data)


def mark_validation_failure(kind, error_type='VALIDATION_ERROR'):
    # Never overwrite another request's provider fact. Detailed bot validators
    # emit their own request-scoped codes; this remains a compatibility hook.
    from ai_diagnostics import CURRENT, reject
    current = CURRENT.get()
    if current and current['kind'] == kind:
        reject(['CONTACT_INVALID' if error_type == 'CONTACT_VALIDATION' else 'UNKNOWN_VALIDATION'])
