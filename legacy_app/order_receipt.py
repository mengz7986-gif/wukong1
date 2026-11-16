"""Display-only committed order facts; no business writes or retries."""
from decimal import Decimal, ROUND_HALF_UP
import logging
import html
logger=logging.getLogger(__name__)

def receipt_chunks(rows, limit=3500):
    # Rendered UTF-16 units are conservatively bounded; split decoded text,
    # then escape each chunk so an HTML entity is never broken in half.
    text=html.unescape('\n'.join(rows))
    chunks=[];current=[];size=0
    for char in text:
        units=2 if ord(char)>0xffff else 1
        if size+units>limit:
            chunks.append(html.escape(''.join(current)));current=[];size=0
        current.append(char);size+=units
    if current:chunks.append(html.escape(''.join(current)))
    return chunks

def money(value, fixed=False):
    amount=Decimal(str(value or 0)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
    if not amount.is_finite():raise ValueError('INVALID_RECEIPT_AMOUNT')
    return format(amount,'.2f') if fixed else format(amount,'.2f').rstrip('0').rstrip('.')

def financial_rows(order):
    rows=['该笔订单：'+money(order['total_amount'])+'+'+money(order['total_shipping_fee'])+'='+money(order['grand_total'])]
    summary=order.get('daily_summary')
    try:
        count=int(summary['today_count'])
        if count<0:raise ValueError('INVALID_COUNT')
        rows.append('今日订单金额：'+str(count)+'单/¥'+money(summary['today_amount'],fixed=True))
    except (TypeError,KeyError,ValueError,ArithmeticError):
        rows.append('今日订单金额：暂时无法读取，请查看下单记录')
    rows.append('总计欠款金额：¥'+money(order['user_debt'],fixed=True))
    return rows

def committed_daily_summary(session,user_id,receipt_only=False):
    # Called only after create_order has committed and the API session refreshed.
    # Never let an auxiliary read failure change the successful order outcome.
    try:
        from telegram_accounts import receipt_daily_summary,summary
        return receipt_daily_summary(session,user_id) if receipt_only else summary(session,user_id)
    except Exception as error:
        logger.warning('RECEIPT_DAILY_SUMMARY_UNAVAILABLE kind=%s',type(error).__name__)
        return None


def customer_failure_reason(error):
    """Display only; never changes pricing, validation or transaction outcome."""
    import re
    reasons=[]
    for part in str(error or '').split('; '):
        if re.search(r'缺货|库存不足|暂不出货|预警库存|库存为\s*0',part):
            ref=re.search(r'品类\(([^()]*)\)口味\(([^()]*)\)',part)
            if ref:
                label=ref[1]+'：'+ref[2]
            else:
                match=re.search(r'TG机器人暂不出货[:：]\s*([^（(\n]+)',part)
                label=match[1].strip().replace('-', '：', 1) if match else None
            value='商品 '+label+' 该口味已缺货' if label else '商品口味库存不足，请调整商品后重新发送完整订单'
        else:
            value=part.strip()
        if value and value not in reasons:reasons.append(value)
    return '；'.join(reasons)
