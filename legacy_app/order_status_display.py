"""Display-only compatibility for existing order states; no state migration."""
def business_status(order):
    status = order.get('status') if isinstance(order, dict) else getattr(order, 'status', None)
    if status == 'cancelled': return '已取消'
    if status == 'shipped': return '已发货'
    if status == 'split_pending': return '分单订单'
    get = order.get if isinstance(order, dict) else lambda key, default=None: getattr(order, key, default)
    if status in ('confirmed', 'recorded') or get('fhd_push_count', 0) or get('fhd_push_status') in ('success', 'partial'):
        return '已录单'
    return '待确认'
