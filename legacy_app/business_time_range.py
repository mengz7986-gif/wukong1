"""UTC+8 business input -> UTC-naive half-open database bounds."""
from datetime import datetime,timedelta,timezone
import re

TIMEZONE='Asia/Shanghai'
OFFSET=timedelta(hours=8)  # Compatible with the existing business-day offset.
BUSINESS_ZONE=timezone(OFFSET,TIMEZONE)

def business_now():
    return datetime.now(BUSINESS_ZONE)

def business_format(value):
    """Existing naive model timestamps are UTC, never browser/server local time."""
    if value is None:return ''
    if isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00'))
    if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
    return value.astimezone(BUSINESS_ZONE).strftime('%Y-%m-%d %H:%M:%S')

def boundary(value,end=False):
    if value is None or value=='':return None
    if not isinstance(value,str):raise ValueError('日期时间必须为文字')
    value=value.strip()
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            local=datetime.strptime(value,'%Y-%m-%d')
            if end:local+=timedelta(days=1)
        elif re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}',value):
            local=datetime.strptime(value.replace('T',' '),'%Y-%m-%d %H:%M:%S')
            if end:local+=timedelta(seconds=1)
        else:raise ValueError()
        return local-OFFSET
    except (ValueError,OverflowError):
        raise ValueError('日期时间格式无效，应为 YYYY-MM-DD 或 YYYY-MM-DD HH:mm:ss（北京时间，24小时制）') from None

def bounds(start=None,end=None,required=False):
    first,last=boundary(start),boundary(end,end=True)
    if required and (first is None or last is None):raise ValueError('请选择开始与结束日期时间')
    if first is not None and last is not None and first>=last:
        raise ValueError('开始时间不能晚于结束时间')
    return first,last
