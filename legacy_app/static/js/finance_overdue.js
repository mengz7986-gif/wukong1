function groupOverdueOrders(orders) {
  const groups = new Map();
  (orders || []).forEach(o => {
    const key = String(o.user_id == null ? o.user_telegram_id : o.user_id);
    if (!groups.has(key)) groups.set(key, {user:o, orders:[], due:0, days:0});
    const g = groups.get(key);
    g.orders.push(o); g.due += Number(o.due_amount || 0);
    g.days = Math.max(g.days, Number(o.overdue_days || 0));
  });
  return Array.from(groups.values());
}
function loadOverdue(){
  $.get('/api/finance/overdue?days=2', function(d){
    const groups=groupOverdueOrders(d.orders);
    $('#overdueSummary').text('共 '+groups.length+' 人 / '+groups.reduce((n,g)=>n+g.orders.length,0)+' 单，合计欠款 ¥'+groups.reduce((n,g)=>n+g.due,0).toFixed(2));
    const tb = $('#overdue-tbody').empty();
    groups.forEach(function(g,index){
      const o=g.user,id='overdue-details-'+index;
      tb.append('<tr>'+
        '<td>'+ escapeFinanceHtml(o.user_name||'') + '<div class="sk-meta">'+escapeFinanceHtml(o.user_telegram_id||'')+'</div></td>'+
        '<td>'+ g.orders.length + ' 单</td>'+
        '<td class="text-danger">¥'+ g.due.toFixed(2) + '</td>'+
        '<td>'+ g.days + ' 天</td>'+
        '<td><button type="button" class="btn btn-sm btn-outline-secondary overdue-toggle" aria-expanded="false" data-target="'+id+'">展开明细</button></td>'+
      '</tr>');
      const details=g.orders.map(o=>'<tr><td>'+escapeFinanceHtml(o.order_number||'')+'</td><td>¥'+Number(o.due_amount||0).toFixed(2)+'</td><td>'+Number(o.overdue_days||0)+' 天</td><td>'+escapeFinanceHtml(RainbowBusinessTime.format(o.created_at))+'</td></tr>').join('');
      tb.append('<tr id="'+id+'" hidden><td colspan="5"><table class="table table-sm"><thead><tr><th>订单</th><th>欠款</th><th>逾期</th><th>下单时间</th></tr></thead><tbody>'+details+'</tbody></table></td></tr>');
    });
    if (!(d.orders||[]).length) {
      tb.append('<tr><td colspan="5" class="text-center text-muted">暂无逾期未付</td></tr>');
    }
  }).fail(function(){
    $('#overdueSummary').text('预警加载失败');
  });
}
$(function(){
  $('#overdue-tbody').on('click','.overdue-toggle',function(){
    const row=document.getElementById(this.dataset.target); row.hidden=!row.hidden;
    this.setAttribute('aria-expanded',String(!row.hidden));this.textContent=row.hidden?'展开明细':'收起明细';
  });loadOverdue();
});
