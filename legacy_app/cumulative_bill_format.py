"""Validated cumulative bill presentation; no database or sending side effects."""
from business_time_range import BUSINESS_ZONE
from decimal import Decimal, InvalidOperation

def render(mention, cutoff, *, current, settled, prepaid, pending, note=None):
    if not mention or cutoff.tzinfo is None:
        raise ValueError('账单缺少已核验用户身份或带时区截止时间')
    amounts = []
    for value in (current, settled, prepaid, pending):
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            raise ValueError('账单金额无效') from None
        if not amount.is_finite() or amount < 0:
            raise ValueError('账单金额必须是有限非负值')
        amounts.append(amount.quantize(Decimal('0.01')))
    current, settled, prepaid, pending = amounts
    if current - settled != pending:
        raise ValueError('累计账单与已结、待结金额不守恒，禁止发送')
    local = cutoff.astimezone(BUSINESS_ZONE)
    lines = [mention]
    if note:
        import html
        lines.append('操作备注：' + html.escape(str(note)))
    lines.extend(['', '🧾 账单', local.strftime('截止到%m月%d日%H时%M分%S秒'),
                  f'当前账单：¥{current:.2f}', f'已结货款：¥{settled:.2f}',
                  f'预存金额：¥{prepaid:.2f}', f'总计待结货款：¥{pending:.2f}'])
    return '\n'.join(lines)
