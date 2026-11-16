"""Read-only Beijing-day metrics. Attempts and order memberships are distinct."""
from sqlalchemy import func,or_
from business_time_range import boundary,business_now
from models import Order,OrderItem,OrderExportBatch,OrderExportItem,Product,User

def metrics(session,day=None,*,warehouse_id=None,logical_category_id=None,view=None,search=None,payment_status=None):
    day=(day or business_now().strftime('%Y-%m-%d')).strip();start=boundary(day);end=boundary(day,end=True)
    query=session.query(Order.id)
    if warehouse_id is not None:query=query.filter(Order.items.any(OrderItem.warehouse_id==warehouse_id))
    if logical_category_id is not None:
        import logical_categories
        query=logical_categories.filter_orders(query,session,logical_category_id,warehouse_id)
    if search:
        pattern='%'+search.strip()+'%'
        query=query.filter(or_(Order.order_number.ilike(pattern),Order.business_order_number.ilike(pattern),Order.recipient_name.ilike(pattern),Order.recipient_phone.ilike(pattern),Order.recipient_address.ilike(pattern),Order.user.has(or_(User.username.ilike(pattern),User.telegram_id.ilike(pattern),User.telegram_name.ilike(pattern),User.display_name.ilike(pattern)))))
    due=func.coalesce(Order.grand_total,0)-func.coalesce(Order.paid_amount,0)
    if payment_status in ('unpaid','partial'):query=query.filter(due>.009)
    elif payment_status=='paid':query=query.filter(due<=.009)
    recorded=or_(Order.recorded_at.is_not(None),Order.status.in_(['confirmed','recorded']),Order.fhd_push_count>0,Order.fhd_push_status.in_(['success','partial']))
    if view=='pending':query=query.filter(Order.status=='pending',Order.recorded_at.is_(None),or_(Order.fhd_push_count.is_(None),Order.fhd_push_count<=0),Order.fhd_push_status.notin_(['success','partial']))
    elif view=='recorded':query=query.filter(Order.status.notin_(['split_pending','shipped','cancelled']),recorded)
    elif view in ('shipped','cancelled'):query=query.filter(Order.status==view)
    elif view=='split':query=query.filter(Order.status=='split_pending')
    membership=Order.id.in_(query)
    from split_management import pending_date_expression
    pending_at=pending_date_expression()
    pending=session.query(func.count(Order.id)).filter(membership,pending_at>=start,pending_at<end,Order.recorded_at.is_(None),Order.status=='pending',or_(Order.fhd_push_count.is_(None),Order.fhd_push_count<=0),Order.fhd_push_status.notin_(['success','partial'])).scalar() or 0
    recorded_count=session.query(func.count(Order.id)).filter(membership,Order.recorded_at>=start,Order.recorded_at<end,Order.status!='cancelled').scalar() or 0
    # Completion, not initiation, owns the day's export facts. Legacy rows
    # without completion retain their creation timestamp as explicit fallback.
    at=func.coalesce(OrderExportBatch.completed_at,OrderExportBatch.created_at)
    batches=session.query(OrderExportBatch).filter(at>=start,at<end)
    filtered=warehouse_id is not None or logical_category_id is not None or bool(search) or view not in (None,'','all') or payment_status not in (None,'','all')
    if filtered:batches=batches.filter(session.query(OrderExportItem.id).filter(OrderExportItem.batch_id==OrderExportBatch.id,OrderExportItem.order_id.in_(query)).exists())
    rows=batches.all();success=[b for b in rows if b.result=='success'];failed=[b for b in rows if b.result=='failed']
    item_query=session.query(OrderExportItem).join(OrderExportBatch,OrderExportBatch.id==OrderExportItem.batch_id).filter(at>=start,at<end,OrderExportBatch.result=='success')
    if filtered:item_query=item_query.filter(OrderExportItem.order_id.in_(query))
    items=item_query.all()
    # Historical aggregate-only rows still have evidence at the batch level;
    # never invent warehouse membership for a failed or legacy export.
    unscoped_failure=session.query(func.count(OrderExportBatch.id)).filter(at>=start,at<end,OrderExportBatch.result=='failed',~session.query(OrderExportItem.id).filter(OrderExportItem.batch_id==OrderExportBatch.id).exists()).scalar() or 0
    duplicate_batches=len({i.batch_id for i in items if i.was_duplicate}) if filtered else sum(b.duplicate_count>0 for b in success)
    return dict(day=day,timezone='Asia/Shanghai',today_pending_recording=int(pending),today_recorded=int(recorded_count),successful_export_batches=len(success),duplicate_export_batches=duplicate_batches,successful_export_orders=len(items) if filtered else sum(b.order_count for b in success),duplicate_export_orders=sum(bool(i.was_duplicate) for i in items) if filtered else sum(b.duplicate_count for b in success),failed_export_batches=None if filtered and unscoped_failure else len(failed),unattributed_failed_export_batches=int(unscoped_failure) if filtered else 0,basis='北京时间自然日；待录按分单释放自然日/普通单创建时间，已录按首次录单时间，导出按完成时间；导出次数为批次',scope=dict(warehouse_id=warehouse_id,logical_category_id=logical_category_id,view=view or 'all',payment_status=payment_status or 'all',search_applied=bool(search)),time_basis=dict(pending='coalesce(split_release_days.day@Asia/Shanghai,orders.created_at)',recorded='orders.recorded_at',export='coalesce(order_export_batches.completed_at,created_at)'),legacy_completion_fallback_batches=sum(b.completed_at is None for b in rows))
