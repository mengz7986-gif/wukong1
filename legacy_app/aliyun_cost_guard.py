"""Local aggregate guard for every paid Alibaba Cloud request.

This is deliberately independent of Alibaba billing alerts: a request reserves
money under an exclusive lock *before* network I/O.  Estimates are conservative
and remain charged when the upstream outcome/cost is unknown.
"""
import fcntl
import json
import os
import secrets
import tempfile
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

MONTHLY_LIMIT = Decimal('1000')
WARNINGS = (Decimal('800'), Decimal('950'))
# Account funding is managed by the owner. Cumulative usage is telemetry,
# not an account-balance proxy or an independent spending stop condition.
CASH_LIMIT = None
CASH_WARNINGS = (Decimal('64'),)
SERVICES = frozenset(('order_ai', 'contact_ai', 'flavor_ai', 'address_structure',
                      'address_complete', 'daily_learning', 'other_ai'))


class BudgetExceeded(RuntimeError):
    pass


def _write(target, value):
    fd,name=tempfile.mkstemp(prefix='.aliyun-cost-',dir=str(target.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as output:
            json.dump(value,output,ensure_ascii=False);output.flush();os.fsync(output.fileno())
        os.chmod(name,0o600);os.replace(name,target)
    finally:
        if os.path.exists(name):os.unlink(name)


def path():
    return Path(os.getenv('RAINBOW_ALIYUN_COST_PATH') or Path(__file__).with_name('aliyun_cost_ledger.json'))


def _now():
    return datetime.now(ZoneInfo('Asia/Shanghai'))


def _money(value, *, maximum=Decimal('1000000000')):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('ALIYUN_COST_INVALID') from None
    if not amount.is_finite() or amount < 0 or amount > maximum:
        raise ValueError('ALIYUN_COST_INVALID')
    return amount.quantize(Decimal('0.000001'))


def _empty(month):
    return {'version': 1, 'month': month, 'spent_cny': '0', 'services': {},
            'days': {}, 'reservations': {}, 'warnings_emitted': [], 'circuits': {}}


def _load(target, month):
    try:
        data = json.loads(target.read_text()) if target.exists() else _empty(month)
    except (OSError, ValueError, TypeError):
        raise BudgetExceeded('ALIYUN_COST_LEDGER_UNAVAILABLE') from None
    if not isinstance(data, dict):raise BudgetExceeded('ALIYUN_COST_LEDGER_INVALID')
    if data.get('version')==2 and not isinstance(data.get('funds'),dict):raise BudgetExceeded('ALIYUN_FUNDS_LEDGER_INVALID')
    try:
        _money(data.get('spent_cny', '0'))
    except ValueError:
        raise BudgetExceeded('ALIYUN_COST_LEDGER_INVALID') from None
    # A calendar rollover must not manufacture new cash. Conservatively carry
    # the legacy month estimate into the non-resetting envelope on first use.
    funds=data.get('funds',{'spent_cny':data.get('spent_cny','0'),'warnings_emitted':[]})
    if not isinstance(funds,dict) or not isinstance(funds.get('warnings_emitted',[]),list):raise BudgetExceeded('ALIYUN_FUNDS_LEDGER_INVALID')
    try:_money(funds.get('spent_cny'))
    except ValueError:raise BudgetExceeded('ALIYUN_FUNDS_LEDGER_INVALID') from None
    if _money(funds['spent_cny'])<_money(data['spent_cny']):raise BudgetExceeded('ALIYUN_FUNDS_LEDGER_INVALID')
    funds.setdefault('warnings_emitted',[])
    if data.get('month')!=month:data=_empty(month)
    data['funds']=funds
    data['version']=2
    return data


def _locked_update(change):
    target = path(); target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(target)+'.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = _now(); data = _load(target, now.strftime('%Y-%m'))
        result = change(data, now)
        _write(target, data)
        return result


def reserve(service, estimated_cny):
    """Atomically reserve one paid call; returns an opaque settlement token."""
    if service not in SERVICES:
        raise ValueError('ALIYUN_COST_SERVICE_INVALID')
    amount = _money(estimated_cny, maximum=MONTHLY_LIMIT)
    if amount <= 0:
        raise ValueError('ALIYUN_COST_RESERVE_REQUIRED')
    def change(data, now):
        spent = _money(data['spent_cny'])
        circuit=data.setdefault('circuits', {}).get(service, {})
        try:blocked_until=datetime.fromisoformat(circuit.get('blocked_until',''))
        except (TypeError,ValueError):blocked_until=None
        if blocked_until and blocked_until > now:
            raise BudgetExceeded('ALIYUN_COST_CIRCUIT_OPEN')
        if spent + amount > MONTHLY_LIMIT:
            raise BudgetExceeded('ALIYUN_MONTHLY_BUDGET_EXHAUSTED')
        cash_spent=_money(data['funds']['spent_cny'])
        token = secrets.token_hex(16)
        data['spent_cny'] = str(spent + amount)
        data['reservations'][token] = {'service': service, 'reserved_cny': str(amount),
                                       'day': now.date().isoformat(), 'settled': False,'funds_reserved':True}
        data['funds']['spent_cny']=str(cash_spent+amount)
        service_row = data['services'].setdefault(service, {'calls': 0, 'cost_cny': '0'})
        service_row['calls'] = int(service_row.get('calls', 0)) + 1
        service_row['cost_cny'] = str(_money(service_row.get('cost_cny', '0')) + amount)
        day = data['days'].setdefault(now.date().isoformat(), {'calls': 0, 'cost_cny': '0'})
        day['calls'] = int(day.get('calls', 0)) + 1
        day['cost_cny'] = str(_money(day.get('cost_cny', '0')) + amount)
        for threshold in WARNINGS:
            label = str(threshold)
            if spent < threshold <= spent + amount and label not in data['warnings_emitted']:
                data['warnings_emitted'].append(label)
        _cash_warning(data,cash_spent,cash_spent+amount)
        return token
    return _locked_update(change)

def _cash_warning(data,before,after):
    for threshold in CASH_WARNINGS:
        label=str(threshold)
        if before<threshold<=after and label not in data['funds']['warnings_emitted']:
            data['funds']['warnings_emitted'].append(label)
            logging.getLogger(__name__).warning('ALIYUN_CASH_WARNING threshold_cny=%s spent_or_reserved_cny=%s limit_cny=%s',label,after,CASH_LIMIT)

def cancel_before_send(token):
    """Refund only an explicitly non-dispatched admission, once; not timeouts."""
    def change(data,now):
        row=data.get('reservations',{}).get(token)
        if not isinstance(row,dict) or row.get('settled') or row.get('cancelled_before_send'):return False
        amount=_money(row['reserved_cny'])
        data['spent_cny']=str(_money(data['spent_cny'])-amount)
        data['funds']['spent_cny']=str(_money(data['funds']['spent_cny'])-amount)
        service=data['services'][row['service']];service['calls']-=1;service['cost_cny']=str(_money(service['cost_cny'])-amount)
        day=data['days'][row['day']];day['calls']-=1;day['cost_cny']=str(_money(day['cost_cny'])-amount)
        row.update(cancelled_before_send=True,settled=True,actual_cny='0')
        return True
    return _locked_update(change)


def settle(token, actual_cny=None):
    """Replace a reservation with known actual cost; unknown keeps reserve."""
    if not token or actual_cny is None:
        return
    actual = _money(actual_cny)
    def change(data, now):
        row = data.get('reservations', {}).get(token)
        if not isinstance(row, dict) or row.get('settled'):
            return False
        reserved = _money(row['reserved_cny']); delta = actual-reserved
        data['spent_cny'] = str(_money(data['spent_cny']) + delta)
        before=_money(data['funds']['spent_cny'])
        data['funds']['spent_cny']=str(before+delta)
        _cash_warning(data,before,before+delta)
        service_row=data['services'][row['service']]
        service_row['cost_cny']=str(_money(service_row['cost_cny'])+delta)
        day=data['days'][row['day']];day['cost_cny']=str(_money(day['cost_cny'])+delta)
        row.update(settled=True, actual_cny=str(actual))
        return True
    return _locked_update(change)


def record_outcome(service, success):
    """Small provider circuit: five consecutive failures pause new calls."""
    if service not in SERVICES:return
    def change(data, now):
        row=data.setdefault('circuits', {}).setdefault(service, {'consecutive_failures': 0})
        if success:
            row.update(consecutive_failures=0, blocked_until=None)
        else:
            failures=int(row.get('consecutive_failures',0))+1
            row['consecutive_failures']=failures
            if failures>=5:
                from datetime import timedelta
                row['blocked_until']=(now+timedelta(seconds=60)).isoformat()
    _locked_update(change)


def snapshot():
    """Public, secret-free month/service/day totals and alert state."""
    def read(data, now):
        spent=_money(data['spent_cny'])
        return {'month': data['month'], 'limit_cny': str(MONTHLY_LIMIT),
                'spent_cny': str(spent), 'remaining_cny': str(max(Decimal('0'),MONTHLY_LIMIT-spent)),
                'warning_80': spent >= WARNINGS[0], 'warning_95': spent >= WARNINGS[1],
                'blocked': spent >= MONTHLY_LIMIT, 'services': data['services'],
                'funds':{'limit_cny':None,'spent_or_reserved_cny':str(_money(data['funds']['spent_cny'])),
                    'remaining_cny':None,'hard_limit_enabled':False,'owner_managed_balance':True,
                    'warning_80':_money(data['funds']['spent_cny'])>=CASH_WARNINGS[0],
                    'warning_95':False,
                    'resets_monthly':False,'scope':'本系统阿里云调用累计预留，不是云端实时余额'},
                'days': data['days'], 'circuits': data.get('circuits',{}), 'unsettled_reservations': sum(
                    not bool(r.get('settled')) for r in data['reservations'].values() if isinstance(r, dict))}
    return _locked_update(read)


def estimate_for(service):
    """Conservative per-call reservation, configurable only below the total cap."""
    key='RAINBOW_ALIYUN_'+service.upper()+'_RESERVE_CNY'
    return _money(os.getenv(key, '0.20'), maximum=MONTHLY_LIMIT)

