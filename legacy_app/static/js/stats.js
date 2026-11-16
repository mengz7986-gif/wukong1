function escapeStatsHtml(v) { return $('<span>').text(v == null ? '' : String(v)).html(); }
function statsDates() {
  return RainbowTimeRange.values('shippingStartDate','shippingEndDate',true);
}
function shipmentQuery() {
  const range = statsDates(), warehouse = $('#statsWarehouse').val();
  return $.param({...range, ...(warehouse ? {warehouse_id: warehouse} : {})});
}
function loadPeriodStats() {
  let query;
  try { query = shipmentQuery(); } catch (e) { alert(e.message); return; }
  $.get('/api/stats/shipments?' + query, function(d) {
    const amount = v => '¥' + Number(v || 0).toFixed(2);
    const cards = [
      ['统计区间', escapeStatsHtml(d.label), `订单 ${d.orders_count} 单`],
      ['商品销售额', amount(d.orders_amount), '不含运费'],
      ['订单总额', amount(d.grand_amount), '其中运费 ' + amount(d.shipping_amount)],
      ['商品利润', d.profit_amount == null ? '历史进货价缺失' : amount(d.profit_amount), '商品销售额 − 销售成本；运费不计利润'],
      ['出货数量', d.shipped_quantity + '支', '按订单生成时间统计有效明细，取消订单不计入'],
      ['当前库存货值', amount(d.stock_reference.reference_value), `已计价 ${d.stock_reference.priced_quantity}件 / 缺价 ${d.stock_reference.missing_quantity}件 / 待确认 ${d.stock_reference.pending_quantity}件`]
    ];
    $('#summaryCards').html(cards.map(c => `<div class="col-md-4"><div class="sk-table-card p-3"><div class="sk-meta">${c[0]}</div><h4>${c[1]}</h4><div class="sk-meta">${c[2]}</div></div></div>`).join(''));
    $('#valueBox').html((d.stock_reference.warehouses || []).map(w => `<div class="mb-2">${escapeStatsHtml(w.name)}：${amount(w.reference_value)}（已计价 ${w.priced_quantity}件 / 缺价 ${w.missing_quantity}件 / 待确认 ${w.pending_quantity}件）</div>`).join('') || '暂无库存货值');
    $('#salesBreakdownSummary').text(`共 ${d.shipped_quantity} 支 · ${d.orders_count} 单 · 商品销售额 ${amount(d.orders_amount)}`);
    $('#bundleSalesSummary').remove();
    if ((d.bundle_sales||[]).length) $('#salesBreakdownSummary').after('<div id="bundleSalesSummary" class="p-3 border rounded my-2"><strong>福袋销售（份数，不重复计入出货支数）</strong>'+d.bundle_sales.map(b=>'<div>'+escapeStatsHtml(b.name)+'：'+b.quantity+'份 · '+Number(b.orders_count||0)+'单 · '+amount(b.amount)+'</div>').join('')+'</div>');
    $('#salesBreakdownBox').html((d.categories || []).map(g => `<details class="p-3 border-bottom"><summary class="fw-bold">${escapeStatsHtml(g.name)}　${g.quantity}支 · ${Number(g.orders_count || 0)}单</summary><div class="pt-2">${g.items.map(i => `<div class="d-flex justify-content-between ps-4 py-1"><span>${escapeStatsHtml(i.product_name)}</span><span>${i.quantity}支 · ${Number(i.orders_count || 0)}单</span></div>`).join('')}</div></details>`).join('') || '<div class="p-3 text-muted">该订单生成区间暂无出货明细</div>');
  }).fail(xhr => alert(typeof xhr.responseJSON?.detail === 'string' ? xhr.responseJSON.detail : '统计加载失败'));
}
function exportShippingOrders() {
  try { window.location.href = '/api/stats/shipments/export?' + shipmentQuery(); }
  catch (e) { alert(e.message); }
}
$(function() {
  const today = RainbowBusinessTime.today();
  $('#shippingStartDate').val(today.slice(0, 8) + '01');
  $('#shippingEndDate').val(today);
  $.get('/api/warehouses', function(data) {
    const rows = (Array.isArray(data) ? data : (data.warehouses || [])).filter(w => w.is_active !== false);
    $('#statsWarehouse').html('<option value="">全部仓库</option>' + rows.map(w => `<option value="${Number(w.id)}">${escapeStatsHtml(w.name)}</option>`).join(''));
    loadPeriodStats();
  }).fail(() => alert('系统仓库加载失败，请刷新重试'));
});
