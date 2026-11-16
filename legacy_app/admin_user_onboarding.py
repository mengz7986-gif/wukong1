"""Explicit backend-only onboarding; shared bot registration never auto-authorizes."""
import re
from datetime import datetime
from models import User,DropshipAuthRequest

def profile(telegram_id,token,post=None,diagnostic=None):
    import requests
    def fail(code,message,http_status=None,retryable=False):
        if diagnostic is not None:
            diagnostic.update(code=code,message=message,http_status=http_status,retryable=retryable)
        return None
    try:
        response=(post or requests.post)('https://api.telegram.org/bot'+token+'/getChat',
            data={'chat_id':telegram_id},timeout=(3,6))
        value=response.json();user=value.get('result') or {}
        if not value.get('ok'):
            status=value.get('error_code') or getattr(response,'status_code',None)
            description=str(value.get('description') or '').casefold()
            if status==400 and 'chat not found' in description:
                return fail('CHAT_NOT_FOUND','Telegram 明确返回 400：chat not found。当前机器人无法按该 ID 查询私聊资料；不是接口超时。可能尚无可访问的私聊记录或 ID 已不可用，不能仅凭用户名推断身份。',400)
            if status==429:return fail('RATE_LIMITED','Telegram 查询限流（429），请稍后重试；不是用户不存在。',429,True)
            if status==401:return fail('BOT_AUTH_FAILED','机器人身份验证失败（401），请管理员检查机器人配置。',401)
            if status==403:return fail('ACCESS_DENIED','Telegram 拒绝资料访问（403），不是网络超时。',403)
            return fail('TELEGRAM_ERROR','Telegram 返回查询错误，原资料未修改。',status,bool(status and status>=500))
        if str(user.get('id'))!=str(telegram_id) or user.get('type')!='private':
            return fail('IDENTITY_MISMATCH','Telegram 返回的 ID 或会话类型不匹配，已拒绝写入资料。')
        name=' '.join(str(user.get(k) or '').strip() for k in ('first_name','last_name')).strip()
        if not name:return fail('NAME_MISSING','Telegram 返回的资料缺少显示名称，未写入不完整资料。')
        if diagnostic is not None:diagnostic.update(code='OK',message='Telegram 资料已核验',retryable=False)
        return dict(telegram_name=name[:150],username=user.get('username') or None)
    except requests.ConnectTimeout:
        return fail('CONNECT_TIMEOUT','连接 Telegram 超时，未收到 Telegram 的用户查询结果。',retryable=True)
    except requests.ReadTimeout:
        return fail('READ_TIMEOUT','等待 Telegram 响应超时，不能据此判断用户不存在。',retryable=True)
    except requests.Timeout:
        return fail('TIMEOUT','Telegram 查询超时，不能据此判断用户不存在。',retryable=True)
    except requests.RequestException:
        return fail('NETWORK_ERROR','Telegram 网络连接失败，原资料未修改。',retryable=True)
    except Exception:
        return fail('INVALID_RESPONSE','Telegram 返回内容无效，原资料未修改。')

def grant(session,user,note):
    if not user.is_active:raise ValueError('用户已禁用，不自动恢复账号')
    latest=session.query(DropshipAuthRequest).filter_by(user_id=user.id).order_by(
        DropshipAuthRequest.created_at.desc(),DropshipAuthRequest.id.desc()).first()
    if latest and latest.status=='approved':return False
    session.add(DropshipAuthRequest(user_id=user.id,telegram_id=user.telegram_id,
        username=user.username,status='approved',admin_note=note,created_at=datetime.utcnow()))
    session.flush()
    return True

def create(session,telegram_id,identity=None,authorize=True):
    telegram_id=str(telegram_id).strip()
    if not re.fullmatch(r'[1-9][0-9]{4,18}',telegram_id):raise ValueError('请输入有效的数字 Telegram 用户ID')
    user=session.query(User).filter_by(telegram_id=telegram_id).first()
    if user:return user,False  # Re-entering an existing ID cannot change its permissions.
    identity=identity or {}
    user=User(telegram_id=telegram_id,username=identity.get('username'),telegram_name=identity.get('telegram_name'))
    session.add(user);session.flush()
    if authorize:grant(session,user,'后台新增用户默认开通；价格沿用既有等级规则，未修改价格')
    return user,True
