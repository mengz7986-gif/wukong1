// Orders management - StyleKit UI
var allOrders = [];
var orderSelection = new Set();
var orderSelectionToken = null;
var orderSelectionRevision = 0;
var orderFilterEpoch = 0;
var activeOrderParams = {};
var activeOrderFilterKey = null;
function clearOrderSelection() {
    orderSelection.clear(); invalidateOrderSelection(); updateOrderSelection();
}
function invalidateOrderSelection(){orderSelectionToken=null;orderSelectionRevision++;}
function ensureOrderSelection(){
    const d=$.Deferred(),ids=selectedOrderIds(),revision=orderSelectionRevision;
    if(!ids.length){d.reject({responseJSON:{detail:'请先勾选订单'}});return d.promise();}
    if(orderSelectionToken){d.resolve(orderSelectionToken);return d.promise();}
    $.post('/api/orders/selection',{order_ids:ids.join(',')}).done(function(r){
        if(revision!==orderSelectionRevision){d.reject({responseJSON:{detail:'选择已改变，请重新操作'}});return;}
        orderSelectionToken=r.selection_token;d.resolve(orderSelectionToken);
    }).fail(function(xhr){d.reject(xhr);});return d.promise();
}
function updateOrderSelection() {
    $('.order-select').each(function(){this.checked=orderSelection.has(String(this.value));});
    $('#selectAllOrders').prop('checked',allOrders.length>0 && allOrders.every(o=>orderSelection.has(String(o.id))));
    $('#orderSelectionCount').text('固定已选 '+orderSelection.size+' 单（新订单不自动加入）');
}
function selectAllFilteredOrders() {
    if (ordersRequest || orderBatchBusy) return;
    if (!confirm('选择当前全部筛选结果（跨所有页）？此步骤只选择，不执行业务操作。')) return;
    const epoch=orderFilterEpoch,revision=orderSelectionRevision, params=Object.assign({},activeOrderParams,{ids_only:true});
    $.get('/api/orders',params).done(function(r){
        if (epoch!==orderFilterEpoch || revision!==orderSelectionRevision) return;
        invalidateOrderSelection();orderSelection=new Set((r.ids||[]).map(String));orderSelectionToken=r.selection_token;updateOrderSelection();
    }).fail(function(){alert('全量选择失败，请重试');});
}
var editingAddressOrderId = null;
var editingShippingOrderId = null;
var ordersLoadToken = 0;
var ORDERS_PAGE_SIZE = 100;
var ordersOffset = 0;
var ordersTotal = 0;
var ordersHasMore = false;
var ordersRequest = null;
var ordersSearchTimer = null;
var orderFiltersReady = false;
window.addEventListener('pagehide',function(){if(orderFiltersReady)RainbowOrderFilterState.save();});

$(document).ready(function() {
    loadOrderReconciliation();
    loadCancellationRequests();
    const warehousesReady=loadOrderWarehouses();
    const categoriesReady=$.get('/api/products/parents',rows=>LogicalCategories.options($('#orderCategoryFilter'),rows));
    let remainingOptions=2;
    function optionsSettled(){
        if (--remainingOptions) return;
        RainbowTimeRange.mount('orderStartDate','orderEndDate');
        RainbowOrderFilterState.restore();
        ORDERS_PAGE_SIZE=Number($('#ordersPageSize').val())||100;
        orderFiltersReady=true;syncQuickChips();loadOrders();
    }
    warehousesReady.always(optionsSettled);categoriesReady.always(optionsSettled);
    $(document).on('input change','#orderSearch,#orderWarehouseFilter,#orderCategoryFilter,#orderViewFilter,#orderPayFilter,#orderStartDate,#orderEndDate,#orderStartDateTime,#orderEndDateTime,#ordersPageSize',function(){
        if(orderFiltersReady)RainbowOrderFilterState.save();
    });
    $('#orderCategoryFilter').on('change',function(){loadOrders(true);});
    $('#ordersPageSize').on('change',function(){ORDERS_PAGE_SIZE=Number(this.value);loadOrders(true);});
    $('#ordersGoPage').on('click',function(){
        const page=Number($('#ordersPageNumber').val());
        if (!Number.isInteger(page)||page<1||ordersRequest) return;
        ordersOffset=(Math.min(page,Math.max(1,Math.ceil(ordersTotal/ORDERS_PAGE_SIZE)))-1)*ORDERS_PAGE_SIZE;loadOrders();
    });
    $('#orders-tbody').on('change','.order-select',function(){
        if(orderBatchBusy){updateOrderSelection();return;}
        invalidateOrderSelection();if(this.checked)orderSelection.add(String(this.value));else orderSelection.delete(String(this.value));updateOrderSelection();
    });
    $('#orderSearch').on('input', function() {
        clearTimeout(ordersSearchTimer);
        ordersLoadToken++;
        if (ordersRequest) ordersRequest.abort();
        ordersSearchTimer = setTimeout(function() { loadOrders(true); }, 300);
    });
    $('#orderViewFilter').on('change', function() {
        syncQuickChips();
        loadOrders(true);
    });
    $('#orderPayFilter').on('change', function() {
        loadOrders(true);
    });
    $('#orderWarehouseFilter').on('change', function() {
        loadOrders(true);
    });
    $('#ordersPrevPage').on('click', function() { changeOrdersPage(-1); });
    $('#ordersNextPage').on('click', function() { changeOrdersPage(1); });
    $(document).on('change','#orderStartDate,#orderEndDate,#orderStartDateTime,#orderEndDateTime', function() {
        loadOrders(true);
    });
    $('#orderQuickFilters').on('click', '.sk-chip', function() {
        var view = $(this).data('view');
        $('#orderViewFilter').val(view);
        if (view === 'cancelled') {
            $('#orderPayFilter').val('all');
        }
        syncQuickChips();
        loadOrders(true);
    });
    $('#editOrderAddressForm').on('submit', submitOrderAddressEdit);
    $('#editOrderShippingForm').on('submit', submitOrderShippingEdit);
    $('#editShippingFeeValue').on('input change', updateShippingFeePreview);
});

function syncQuickChips() {
    var status = $('#orderViewFilter').val() || 'all';
    $('#orderQuickFilters .sk-chip').removeClass('active');
    $('#orderQuickFilters .sk-chip[data-view="' + status + '"]').addClass('active');
}

function loadOrders(force) {
    if(!orderFiltersReady)return;
    RainbowOrderFilterState.save();
    clearTimeout(ordersSearchTimer);
    var loadToken = ++ordersLoadToken;
    if (ordersRequest) ordersRequest.abort();
    if (force) {ordersOffset = 0;}
    $('#loading').html('<div class="spinner-border" role="status"></div><div class="sk-empty-desc mt-2">正在加载订单...</div>').show();
    $('#orders-content').hide();
    var status = ($('#orderViewFilter').val() || 'all');
    var range;
    try{range=RainbowTimeRange.values('orderStartDate','orderEndDate');}catch(e){alert(e.message);return;}
    var startDate=range.start,endDate=range.end;
    $('#orderStartDate').closest('.orders-filter-field').find('.rrp-trigger span').text(
        startDate&&endDate ? startDate.replace('T',' ')+' — '+endDate.replace('T',' ') : '选择日期时间范围');
    var warehouseId = ($('#orderWarehouseFilter').val() || '').trim();
    var params = {
        limit: ORDERS_PAGE_SIZE,
        offset: ordersOffset,
        include_meta: true,
        include_receipt_evidence: true,
        search: ($('#orderSearch').val() || '').trim(),
        payment_status: status === 'cancelled' ? 'all' : ($('#orderPayFilter').val() || 'all')
    };
    if (status !== 'all') params.view = status;
    if (startDate) params.start_date = startDate;
    if (endDate) params.end_date = endDate;
    if (warehouseId) params.warehouse_id = warehouseId;
    const categoryId=$('#orderCategoryFilter').val();
    if(categoryId)params.logical_category_id=categoryId;
    activeOrderParams=Object.assign({},params);
    const filterParams=Object.assign({},params);delete filterParams.limit;delete filterParams.offset;delete filterParams.include_meta;
    const filterKey=JSON.stringify(filterParams);
    if(activeOrderFilterKey!==filterKey){activeOrderFilterKey=filterKey;orderFilterEpoch++;clearOrderSelection();}
    ordersRequest = $.ajax({
        url: '/api/orders',
        data: params,
        method: 'GET',
        timeout: 20000,
        success: function(response) {
            if (loadToken !== ordersLoadToken) return;
            allOrders = Array.isArray(response) ? response : (response.orders || []);
            var meta = response.meta || {};
            ordersTotal = Number(meta.total == null ? allOrders.length : meta.total);
            ordersHasMore = !!meta.has_more;
            if (!allOrders.length && ordersOffset > 0 && ordersTotal > 0) {
                ordersOffset = Math.floor((ordersTotal - 1) / ORDERS_PAGE_SIZE) * ORDERS_PAGE_SIZE;
                loadOrders();
                return;
            }
            renderOrders();
            loadOrderReconciliation();
            updateOrderSelection();
            $('#loading').hide();
            $('#orders-content').show();
        },
        error: function(xhr, textStatus) {
            if (loadToken !== ordersLoadToken || textStatus === 'abort') return;
            var detail = (xhr.responseJSON && xhr.responseJSON.detail) || '请点击刷新重试';
            $('#loading').html('<div class="sk-empty-title">加载失败</div><div class="sk-empty-desc">' + escapeHtml(String(detail)) + '</div>');
        },
        complete: function() {
            if (loadToken === ordersLoadToken) ordersRequest = null;
        }
    });
}

function changeOrdersPage(direction) {
    if (ordersRequest || (direction > 0 && !ordersHasMore)) return;
    ordersOffset = Math.max(0, ordersOffset + direction * ORDERS_PAGE_SIZE);
    loadOrders();
}

function loadOrderWarehouses() {
    return $.get('/api/warehouses')
        .done(function(rows) {
            var select = $('#orderWarehouseFilter');
            (rows || []).forEach(function(row) {
                if (!row || row.id == null) return;
                select.append($('<option>').val(row.id).text(row.name || ('仓库 ' + row.id)));
            });
        })
        .fail(function(xhr) { console.error('Failed to load warehouses:', xhr); });
}

function loadCancellationRequests() {
    $.get('/api/order-cancellation-requests?status=pending&limit=200')
        .done(function(resp) {
            var rows = (resp && resp.requests) || [];
            var card = $('#cancellationRequestsCard');
            var tbody = $('#cancellationRequestsTbody');
            tbody.empty();
            card.toggle(rows.length > 0);
            $('#cancellationRequestsHint').text(rows.length ? (rows.length + ' 条待处理申请') : '暂无待处理申请');
            rows.forEach(function(item) {
                var customer = item.display_name || item.username || item.telegram_id || '-';
                var reason = item.reason || '客户未填写原因';
                var orderNumber = item.order_number || item.order_id || '-';
                var row = '<tr>' +
                    '<td><div class="sk-strong">' + escapeHtml(orderNumber) + '</div><div class="sk-meta">' + escapeHtml(item.order_status || '') + '</div></td>' +
                    '<td>' + escapeHtml(customer) + '<div class="sk-meta">' + escapeHtml(item.telegram_id || '') + '</div></td>' +
                    '<td>￥' + Number(item.grand_total || 0).toFixed(2) + '</td>' +
                    '<td class="sk-addr">' + escapeHtml(reason) + '</td>' +
                    '<td><span class="sk-meta">' + formatDateTime(item.created_at) + '</span></td>' +
                    '<td><div class="sk-actions">' +
                    '<button class="btn btn-sm btn-success" onclick="reviewCancellationRequest(' + Number(item.id) + ', \'approve\')" title="同意取消"><i class="fas fa-check"></i></button>' +
                    '<button class="btn btn-sm btn-outline-danger" onclick="reviewCancellationRequest(' + Number(item.id) + ', \'reject\')" title="驳回申请"><i class="fas fa-times"></i></button>' +
                    '</div></td>' +
                    '</tr>';
                tbody.append(row);
            });
        })
        .fail(function(xhr) { console.error('Failed to load cancellation requests:', xhr); });
}

function exportOrderList() {
    if(orderSelection.size){exportSelectedOrderList();return;}
    var params = [];
    var range;
    try{range=RainbowTimeRange.values('orderStartDate','orderEndDate');}catch(e){alert(e.message);return;}
    var start=range.start,end=range.end;
    var status = $('#orderViewFilter').val() || 'all';
    if (start) params.push('start_date=' + encodeURIComponent(start));
    if (end) params.push('end_date=' + encodeURIComponent(end));
    if (status !== 'all') params.push('view=' + encodeURIComponent(status));
    var warehouseId = ($('#orderWarehouseFilter').val() || '').trim();
    if (warehouseId) params.push('warehouse_id=' + encodeURIComponent(warehouseId));
    var categoryId=$('#orderCategoryFilter').val();
    if(categoryId)params.push('logical_category_id='+encodeURIComponent(categoryId));
    var search = ($('#orderSearch').val() || '').trim();
    var payment = $('#orderPayFilter').val() || 'all';
    if (search) params.push('search=' + encodeURIComponent(search));
    if (status !== 'cancelled' && payment !== 'all') params.push('payment_status=' + encodeURIComponent(payment));
    var base = '/api/orders/export' + (params.length ? '?' + params.join('&') : '');
    var separator = params.length ? '&' : '?';
    $.get(base + separator + 'preflight=true').done(function(resp) {
        if (resp && resp.requires_confirmation) {
            var details = (resp.conflicts || []).map(function(item) {
                return (item.day || '') + ' ' + (item.recipient_name || '') + ' / ' +
                    (item.recipient_address || '') + '\n订单：' + (item.order_numbers || []).join('、');
            }).join('\n\n');
            var warning = '🚨 强提醒：同一地址同一天存在多笔分单。\n' +
                '默认一天只导出一笔，继续将导出全部冲突订单，请人工核对。\n\n' + details +
                '\n\n确认继续导出全部订单吗？';
            if (!confirm(warning)) return;
            window.location.href = base + separator + 'confirm_duplicate_split=true';
            return;
        }
        window.location.href = base;
    }).fail(function(xhr) {
        alert('导出检查失败：' + ((xhr.responseJSON && xhr.responseJSON.detail) || '未知错误'));
    });
}

function exportSelectedOrderList(){
    ensureOrderSelection().done(function(token){
        $.post('/api/orders/selection/export',{selection_token:token,preflight:true}).done(async function(r){
            let warning='';
            if(r.changed_ids.length)warning+='选择后已变更的订单编号：'+r.changed_ids.join('、')+'。将导出同一批编号的最新内容。\n';
            if(r.requires_confirmation)warning+='同一地址同一天有多笔分单，请人工核对。\n';
            if(warning&&!confirm(warning+'继续导出固定批次 '+r.order_count+' 单？'))return;
            try{
                const response=await fetch('/api/orders/selection/export',{method:'POST',body:new URLSearchParams({selection_token:token,confirm_duplicate_split:String(!!r.requires_confirmation)})});
                if(!response.ok)throw new Error('导出失败，请核对固定批次，未下载文件');
                const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');
                a.href=url;a.download='orders_selected.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
            }catch(e){alert(e.message);}
        }).fail(function(xhr){alert(JSON.stringify((xhr.responseJSON&&xhr.responseJSON.detail)||'固定批次导出检查失败'));});
    }).fail(function(xhr){alert((xhr.responseJSON&&xhr.responseJSON.detail)||'固定批次创建失败');});
}

function reviewCancellationRequest(requestId, decision) {
    var action = decision === 'approve' ? '同意取消' : '驳回申请';
    if (!confirm('确定' + action + '吗？')) return;
    var note = prompt('审核备注（可选）', '') || '';
    $.post('/api/order-cancellation-requests/' + requestId + '/review', {
        decision: decision,
        admin_note: note,
        reviewed_by: '后台管理员'
    }).done(function(resp) {
        alert((resp && resp.message) || action + '成功');
        loadOrders(true);
        loadCancellationRequests();
    }).fail(function(xhr) {
        alert(action + '失败: ' + ((xhr.responseJSON && xhr.responseJSON.detail) || '未知错误'));
    });
}

function renderOrders() {
    var status = $('#orderViewFilter').val() || 'all';
    var list = allOrders;
    $('#orderCountPill').text(ordersTotal + ' 单');
    $('#orderHint').text(status === 'cancelled' ? '按取消时间最新在上，服务端分页' : '服务端搜索与筛选，每页最多 ' + ORDERS_PAGE_SIZE + ' 单');
    $('#ordersPagination').css('display', 'flex');
    $('#ordersPaginationHint').text('共 '+ordersTotal+' 单；第 ' + (Math.floor(ordersOffset / ORDERS_PAGE_SIZE) + 1) + ' 页 / 共 ' + Math.max(1, Math.ceil(ordersTotal / ORDERS_PAGE_SIZE)) + ' 页；跨页选择保留，筛选变更清空选择');
    $('#ordersPageNumber').val(Math.floor(ordersOffset / ORDERS_PAGE_SIZE)+1).attr('max',Math.max(1,Math.ceil(ordersTotal/ORDERS_PAGE_SIZE)));
    $('#ordersPrevPage').prop('disabled', ordersOffset === 0);
    $('#ordersNextPage').prop('disabled', !ordersHasMore);

    var tbody = $('#orders-tbody');
    tbody.empty();
    var visibleList = list;

    if (!list.length) {
        $('#ordersEmpty').show();
        $('#ordersTableWrap').hide();
        return;
    }
    $('#ordersEmpty').hide();
    $('#ordersTableWrap').show();

    var rows = visibleList.map(function(order) {
        var statusBadge = getStatusBadge(order.status, order.recorded);
        var payBadge = getPaymentBadge(order.payment_status, order.due_amount);
        var logisticsRecord = getLogisticsRecord(order);
        var orderItems = order.items || [];
        var groupedItems = (order.grouped_items && order.grouped_items.length)
            ? order.grouped_items
            : groupOrderItems(orderItems);
        var totalItemQuantity = groupedItems.reduce(function(total, group) {
            return total + Number(group.quantity || 0);
        }, 0);
        var itemLines = groupedItems.map(function(group) {
            var flavorText = (group.flavors || []).map(function(flavor) {
                return escapeHtml(flavor.product_name || '-') + '×' + Number(flavor.quantity || 0)+(flavor.unit&&flavor.unit!=='支'?escapeHtml(flavor.unit):'');
            }).join('、');
            return '<div class="sk-item-line"><strong>' + escapeHtml(group.category_name || '默认分类') + '</strong>：' + flavorText + '</div>';
        });
        var items = itemLines.join('');
        var sales = order.bundle_sales || [];
        if (sales.length) {
            var linkedIds = new Set();
            sales.forEach(function(sale) { (sale.item_ids || []).forEach(function(id) { linkedIds.add(Number(id)); }); });
            var ordinaryGroups = groupOrderItems(orderItems.filter(function(item) { return !linkedIds.has(Number(item.id)); }));
            items = sales.map(function(sale) {
                return '<div class="sk-item-line"><strong>福袋：' + escapeHtml(sale.name) + '</strong> ×' + Number(sale.quantity) + '份</div>';
            }).join('') + ordinaryGroups.map(function(group) {
                return '<div class="sk-item-line">普通商品：' + escapeHtml(group.category_name) + '：' + (group.flavors || []).map(function(flavor) {
                    return escapeHtml(flavor.product_name) + '×' + Number(flavor.quantity);
                }).join('、') + '</div>';
            }).join('') + '<details><summary class="sk-meta">实际配货明细</summary>' + itemLines.join('') + '</details>';
        }
        var unitTotals={};orderItems.forEach(function(item){var unit=['支','颗','盒','瓶','包','套','件'].includes(item.unit)?item.unit:'支';unitTotals[unit]=(unitTotals[unit]||0)+Number(item.quantity||0);});
        var unitSummary=Object.keys(unitTotals).map(function(unit){return unitTotals[unit]+' '+unit;}).join('、');
        var itemSummary = orderItems.length
            ? '<div class="sk-meta">共 ' + groupedItems.length + ' 个品类 / ' + unitSummary + '</div>'
            : '';
        var userName = order.user_display_name || order.user_telegram_name || order.user_username || order.user_telegram_id || '-';
        var telegramMeta = [
            order.user_display_name && order.user_telegram_name && order.user_display_name !== order.user_telegram_name
                ? '飞机名：' + escapeHtml(order.user_telegram_name)
                : '',
            order.user_username ? '@' + escapeHtml(order.user_username) : '',
            order.user_telegram_id ? 'ID: ' + escapeHtml(order.user_telegram_id) : '',
        ].filter(Boolean).join(' · ');
        var safeUserName = escapeHtml(userName);
        var orderNumberArg = String(order.order_number || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
        var statusArg = String(order.status || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
        var canShip = order.status !== 'shipped' && order.status !== 'cancelled' && order.status !== 'split_pending';
        var canCancel = order.status !== 'cancelled' && order.status !== 'shipped';
        var shipDisabled = canShip ? '' : 'disabled';
        var cancelDisabled = canCancel ? '' : 'disabled';
        var row = `
        <tr>
          <td><input type="checkbox" class="order-select" value="${order.id}"></td>
          <td>
            <div class="sk-strong">#${order.order_number}</div>
            <div class="sk-meta">ID ${order.id}</div>
            ${order.split_group_id ? `<div class="sk-meta text-danger">分单 ${Number(order.split_index || 0)}/${Number(order.split_count || 0)}</div>` : ''}
          </td>
          <td>
            <div>${safeUserName}</div>
            <div class="sk-meta">${telegramMeta}</div>
          </td>
          <td>
            <div class="sk-strong">¥${Number(order.grand_total || 0).toFixed(2)}</div>
            <div class="sk-meta">商品 ${Number(order.total_amount || 0).toFixed(0)} · 运费 ${Number(order.total_shipping_fee || 0).toFixed(0)}</div>
            <div class="sk-meta">已收 ${Number(order.paid_amount || 0).toFixed(0)} · 欠 ${Number(order.due_amount || 0).toFixed(0)}</div>
          </td>
          <td class="sk-items-cell">${items || '<span class="sk-meta">-</span>'}${itemSummary}</td>
          <td><div class="sk-strong">${escapeHtml((order.items || []).map(function(item) { return item.warehouse_name || ''; }).filter(Boolean).filter(function(name, index, names) { return names.indexOf(name) === index; }).join('、') || '-')}</div></td>
          <td>
            <div>${escapeHtml(order.recipient_name || '-')} <span class="sk-meta">${escapeHtml(order.recipient_phone || '')}</span></div>
            <div class="sk-meta sk-addr">${escapeHtml(order.recipient_address || '')}</div>
          </td>
          <td class="sk-money-ok">¥${Number(order.total_profit || 0).toFixed(0)}</td>
          <td>${statusBadge}${receiptDeliveryBadge(order.receipt)}</td>
          <td>${payBadge}</td>
          <td>${logisticsRecord}<div class="mt-1"><span class="badge ${order.recorded ? 'bg-success' : 'bg-secondary'}">${order.recorded ? '已录单' : '未录单'}</span></div></td>
          <td><span class="sk-meta">${formatDateTime(order.status === 'cancelled' ? (order.updated_at || order.created_at) : order.created_at)}</span>${order.status === 'cancelled' ? '<div class="sk-meta">取消时间</div>' : ''}</td>
          <td>
            <div class="sk-actions">
              ${order.status === 'cancelled'
                ? `<button class="btn btn-sm btn-success" onclick="restoreOrder(${order.id})" title="恢复"><i class="fas fa-undo"></i> 恢复</button>`
                : `<button class="btn btn-sm btn-warning" onclick="shipOrder(${order.id})" title="发货" ${shipDisabled}><i class="fas fa-truck"></i> 发货</button>
              <button class="btn btn-sm btn-danger" onclick="cancelOrder(${order.id})" title="取消" ${cancelDisabled}><i class="fas fa-times"></i> 取消</button>`}
            </div>
          </td>
        </tr>`;
        return row;
    });
    tbody.html(rows.join(''));
    $('#selectAllOrders').prop('checked', false);
}

function getStatusBadge(status, recorded) {
    var statusMap = {
        'pending': recorded ? { text: '已录单', class: 'badge-info' } : { text: '待录单', class: 'badge-warning' },
        'split_pending': { text: '分单订单', class: 'badge-danger' },
        'confirmed': { text: '已录单', class: 'badge-info' },
        'shipped': { text: '已发货', class: 'badge-success' },
        'cancelled': { text: '已取消', class: 'badge-danger' }
    };
    var s = statusMap[status] || { text: status, class: 'badge-secondary' };
    return `<span class="badge ${s.class}">${s.text}</span>`;
}

var reconciliationRevision = 0;
var reconciliationRenderedDay = null;
function reconciliationDayNow(){return new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());}
setInterval(function(){if(!document.hidden&&reconciliationRenderedDay&&reconciliationRenderedDay!==reconciliationDayNow())loadOrderReconciliation();},30000);
document.addEventListener('visibilitychange',function(){if(!document.hidden&&reconciliationRenderedDay&&reconciliationRenderedDay!==reconciliationDayNow())loadOrderReconciliation();});
function loadOrderReconciliation() {
    const revision=++reconciliationRevision;
    const view=$('#orderViewFilter').val()||'all';
    const params={warehouse_id:$('#orderWarehouseFilter').val()||undefined,logical_category_id:$('#orderCategoryFilter').val()||undefined,view:view,search:$('#orderSearch').val()||'',payment_status:view==='cancelled'?'all':($('#orderPayFilter').val()||'all')};
    $.ajax({url:'/api/orders/export-reconciliation',data:params,cache:false}).done(function(r) {
        if(revision!==reconciliationRevision)return;
        reconciliationRenderedDay=r.day;
        $('#recPending').text(Number(r.today_pending_recording || 0));
        $('#recRecorded').text(Number(r.today_recorded || 0));
        $('#recSuccess').text(Number(r.successful_export_batches || 0));
        $('#recDuplicate').text(Number(r.duplicate_export_batches || 0));
        $('#recFailed').text(r.failed_export_batches==null?'待核归属':Number(r.failed_export_batches || 0));
        $('#orderReconciliation').attr('title', (r.day || '') + ' · ' + (r.basis || ''));
    }).fail(function() {
        if(revision!==reconciliationRevision)return;
        $('#orderReconciliation strong').text('读取失败');
    });
}

function getPaymentBadge(paymentStatus, dueAmount) {
    var due = Number(dueAmount || 0);
    if (due > 0.009) {
        return `<span class="badge badge-danger">欠 ¥${due.toFixed(2)}</span>`;
    }
    if (paymentStatus === 'paid' || due <= 0) {
        return `<span class="badge badge-success">已结清</span>`;
    }
    return `<span class="badge badge-secondary">${paymentStatus || '未知'}</span>`;
}

function getLogisticsRecord(order) {
    var waybills = Array.isArray(order.fhd_waybill_codes) ? order.fhd_waybill_codes : (order.fhd_waybill_code ? [order.fhd_waybill_code] : []);
    var waybill = waybills.length ? `<div class="sk-meta">单号 ${escapeHtml(waybills.join(', '))}</div>` : '<span class="sk-meta">-</span>';
    var cp = order.fhd_cp_name || order.fhd_cp_code || '';
    var company = cp ? `<div class="sk-meta">${cp}</div>` : '';
    return `${company}${waybill}`;
}

function confirmOrder(orderId) {
    var order = allOrders.find(function(item) { return Number(item.id) === Number(orderId); });
    var action = order && order.status === 'split_pending' ? '确认整组分单订单' : '确认订单';
    updateOrderStatus(orderId, 'confirmed', action);
}
function shipOrder(orderId) { updateOrderStatus(orderId, 'shipped', '发货'); }
function cancelOrder(orderId) {
    openOrderCancellation([String(orderId)]);
}

var orderCancellationTargets = [], orderCancellationBusy = false, orderCancellationRecovery = null;
function cancellationRecovery() {
    if(orderCancellationRecovery)return orderCancellationRecovery;
    var storage=null;try{storage=window.sessionStorage;}catch(error){}
    orderCancellationRecovery=RainbowCancelRecovery.create({
        owner:document.getElementById('orderCancellationRecovery').getAttribute('data-owner'),storage:storage,
        check:ids=>$.ajax({url:'/api/orders/cancellation/check',type:'POST',dataType:'json',timeout:15000,data:{order_ids:ids.join(',')}}),
        submit:(ids,reason)=>$.ajax({url:'/api/orders/batch_update_status',type:'POST',dataType:'json',timeout:15000,data:{order_ids:ids.join(','),status:'cancelled',reason:reason}})
    });
    return orderCancellationRecovery;
}
function renderCancellationRecovery() {
    var task=cancellationRecovery().current();
    $('#orderCancellationRecovery').toggleClass('d-none',!task);
    if(task)$('#orderCancellationRecoveryText').text('原组选单 '+task.ids.length+' 笔已保留。先核对当前结果，已取消自动跳过，仅继续未取消项。');
    $('#orderCancellationResume').toggleClass('d-none',!task);
    $('#orderCancellationSubmit').toggleClass('d-none',!!task);
    $('#orderCancellationReason').prop('readonly',!!task);
}
function openCancellationRecovery() {
    if(orderCancellationBusy || orderBatchBusy)return;
    try {
        var task=cancellationRecovery().current();if(!task)return;
        orderCancellationTargets=task.ids.slice();
        $('#orderCancellationTargets').text('保留的原组选单共 '+task.ids.length+' 笔，不随当前筛选或勾选变化。');
        $('#orderCancellationReason').val(task.reason);
        $('#orderCancellationError').text('点击后先核对全部原组选单；已取消跳过，尚未取消继续处理。');
        renderCancellationRecovery();
        bootstrap.Modal.getOrCreateInstance(document.getElementById('orderCancellationModal')).show();
    }catch(error){alert(error.message || '恢复批次读取失败，未执行取消');}
}
async function continueOrderCancellation() {
    if(orderCancellationBusy || orderBatchBusy)return;
    orderCancellationBusy=true;orderBatchBusy=true;
    $('#orderCancellationModal button, #orderCancellationReason, #orderCancellationRecoveryOpen').prop('disabled',true);
    $('#orderCancellationError').text('正在核对原组选单并处理未取消项，请勿重复点击…');
    try {
        var result=await cancellationRecovery().run();
        if(result.done) {
            orderCancellationBusy=false;
            bootstrap.Modal.getOrCreateInstance(document.getElementById('orderCancellationModal')).hide();
            alert('核对完成：原组选单 '+result.total+' 笔均已取消；此前已取消项未重复回补或冲账。');
        } else {
            $('#orderCancellationError').text('已核实取消 '+result.cancelled+'/'+result.total+' 笔；以下项目仍未取消，原组选单继续保留：'+
                result.remaining.map(row=>'ID '+row.order_id+'（'+row.status+'）').join('、'));
        }
    } catch(error) {
        $('#orderCancellationError').text((error.message || (error.responseJSON&&error.responseJSON.detail) || '核对或取消请求断网/超时，当前结果未知')+
            '。原组选单已保留，请恢复网络后点击“核对结果并继续取消”，勿更换选单盲目重试。');
    } finally {
        orderCancellationBusy=false;orderBatchBusy=false;
        $('#orderCancellationModal button, #orderCancellationReason, #orderCancellationRecoveryOpen').prop('disabled',false);
        renderCancellationRecovery();loadOrders(true);
    }
}
$(document).ready(function(){try{renderCancellationRecovery();}catch(error){$('#orderCancellationError').text(error.message);}});
function openOrderCancellation(ids) {
    if (orderCancellationBusy || orderBatchBusy) return;
    try{if(cancellationRecovery().current()){openCancellationRecovery();return;}}
    catch(error){alert(error.message || '原组选单读取失败，未执行取消');return;}
    ids = Array.from(new Set(ids.map(String)));
    if (!ids.length) { alert('请先勾选订单'); return; }
    orderCancellationTargets = ids.slice();
    var labels = ids.map(function(id) {
        var order = allOrders.find(function(row) { return String(row.id) === id; });
        return order ? order.order_number : ('ID ' + id);
    });
    $('#orderCancellationTargets').text(ids.length === 1 ? ('订单：' + labels[0]) :
        ('共 ' + ids.length + ' 笔订单，共用下方取消原因。订单：' + labels.slice(0, 20).join('、') + (ids.length > 20 ? '…' : '')));
    $('#orderCancellationReason').val('');
    $('#orderCancellationError').text('');
    renderCancellationRecovery();
    bootstrap.Modal.getOrCreateInstance(document.getElementById('orderCancellationModal')).show();
}

function submitOrderCancellation(event) {
    event.preventDefault();
    if (orderCancellationBusy) return;
    var reason = String($('#orderCancellationReason').val() || '').trim();
    if (!reason || Array.from(reason).length > 2000) {
        $('#orderCancellationError').text('请输入1–2000字的取消原因'); return;
    }
    var ids = orderCancellationTargets.slice();
    if (!ids.length) return;
    try {
        if(!cancellationRecovery().current())cancellationRecovery().start(ids,reason);
        renderCancellationRecovery();continueOrderCancellation();
    } catch(error){$('#orderCancellationError').text(error.message || '原组选单保存失败，未执行取消');}
}
$(document).on('hide.bs.modal', '#orderCancellationModal', function(event) {
    if (orderCancellationBusy) event.preventDefault();
});

function updateOrderStatus(orderId, status, action) {
    $.ajax({
        url: `/api/orders/${orderId}/update_status`,
        method: 'POST',
        data: { status: status, reason: action },
        success: function(response) {
            alert((response && response.message) ? response.message : (action + '成功'));
            loadOrders(true);
        },
        error: function(err) {
            alert(action + '失败: ' + ((err.responseJSON && (err.responseJSON.detail || err.responseJSON.message)) || '未知错误'));
        }
    });
}


function openShippingFeeEditor(orderId) {
    var order = allOrders.find(function(item) { return Number(item.id) === Number(orderId); });
    if (!order) {
        alert('未找到订单，请刷新后重试');
        return;
    }
    if (order.status === 'cancelled') {
        alert('已取消订单不能修改运费');
        return;
    }
    editingShippingOrderId = Number(orderId);
    var fee = Number(order.total_shipping_fee || 0);
    var productAmount = Number(order.total_amount || 0);
    var grandTotal = Number(order.grand_total || (productAmount + fee));
    $('#editShippingOrderNumber').text('#' + (order.order_number || order.id));
    $('#editShippingProductAmount').val(productAmount.toFixed(2));
    $('#editShippingCurrentFee').val(fee.toFixed(2));
    $('#editShippingFeeValue').val(fee.toFixed(2));
    $('#editShippingPaidAmount').val(Number(order.paid_amount || 0).toFixed(2));
    updateShippingFeePreview();
    $('#editShippingModal').modal('show');
    setTimeout(function() { $('#editShippingFeeValue').trigger('focus').select(); }, 200);
}

function updateShippingFeePreview() {
    var productAmount = Number($('#editShippingProductAmount').val() || 0);
    var fee = Number($('#editShippingFeeValue').val() || 0);
    if (!isFinite(fee) || fee < 0) fee = 0;
    var paid = Number($('#editShippingPaidAmount').val() || 0);
    var grand = productAmount + fee;
    var due = Math.max(0, grand - paid);
    $('#editShippingGrandPreview').text('¥' + grand.toFixed(2));
    $('#editShippingDuePreview').text('¥' + due.toFixed(2));
}

function submitOrderShippingEdit(event) {
    event.preventDefault();
    if (!editingShippingOrderId) return;
    var feeRaw = ($('#editShippingFeeValue').val() || '').trim();
    var fee = Number(feeRaw);
    if (!isFinite(fee)) {
        alert('请输入有效运费金额');
        return;
    }
    if (fee < 0) {
        alert('运费不能为负数');
        return;
    }
    var $btn = $('#saveOrderShippingBtn');
    $btn.prop('disabled', true);
    $.ajax({
        url: '/api/orders/' + editingShippingOrderId + '/update_shipping_fee',
        method: 'POST',
        data: { total_shipping_fee: fee.toFixed(2) },
        success: function(response) {
            var order = allOrders.find(function(item) { return Number(item.id) === Number(editingShippingOrderId); });
            if (order && response) {
                order.total_shipping_fee = response.total_shipping_fee;
                order.grand_total = response.grand_total;
                order.paid_amount = response.paid_amount;
                order.payment_status = response.payment_status;
                order.due_amount = response.due_amount;
            }
            $('#editShippingModal').modal('hide');
            editingShippingOrderId = null;
            renderOrders();
            alert((response && response.message) || '运费已修改');
        },
        error: function(err) {
            alert('修改运费失败: ' + ((err.responseJSON && (err.responseJSON.detail || err.responseJSON.message)) || '未知错误'));
        },
        complete: function() {
            $btn.prop('disabled', false);
        }
    });
}

function openAddressEditor(orderId) {
    var order = allOrders.find(function(item) { return Number(item.id) === Number(orderId); });
    if (!order) {
        alert('未找到订单，请刷新后重试');
        return;
    }
    if (order.status === 'shipped') {
        alert('订单已发货，收货地址不能再修改');
        return;
    }
    if (order.status === 'cancelled') {
        alert('已取消订单不能修改收货地址');
        return;
    }

    editingAddressOrderId = Number(orderId);
    $('#editAddressOrderNumber').text('#' + (order.order_number || order.id));
    $('#editAddressCurrent').val(order.recipient_address || '');
    $('#editAddressValue').val(order.recipient_address || '');
    $('#editAddressModal').modal('show');
    setTimeout(function() { $('#editAddressValue').trigger('focus').select(); }, 200);
}

function submitOrderAddressEdit(event) {
    event.preventDefault();
    if (!editingAddressOrderId) return;

    var address = ($('#editAddressValue').val() || '').trim().replace(/\s+/g, ' ');
    if (address.length < 4) {
        alert('请填写完整详细地址');
        return;
    }

    var $button = $('#saveOrderAddressBtn');
    $button.prop('disabled', true);
    $.ajax({
        url: `/api/orders/${editingAddressOrderId}/update_address`,
        method: 'POST',
        data: { recipient_address: address },
        success: function(response) {
            var order = allOrders.find(function(item) { return Number(item.id) === Number(editingAddressOrderId); });
            if (order) order.recipient_address = response.recipient_address || address;
            $('#editAddressModal').modal('hide');
            editingAddressOrderId = null;
            alert((response && response.message) ? response.message : '收货地址已修改');
            loadOrders(true);
        },
        error: function(err) {
            alert('修改地址失败: ' + ((err.responseJSON && (err.responseJSON.detail || err.responseJSON.message)) || '未知错误'));
        },
        complete: function() {
            $button.prop('disabled', false);
        }
    });
}

function showOrderDetail(orderId) {
    window.location.href = '/admin/orders/detail?id=' + orderId;
}

function printOrder(orderId, template) {
    window.open('/admin/orders/print?id=' + orderId + '&template=' + (template || 'shipping'), '_blank');
}

function notifyOrder(orderId, eventName) {
    $.ajax({
        url: `/api/orders/${orderId}/notify`,
        method: 'POST',
        data: { event: eventName || 'shipped' },
        success: function() { alert('通知已发送'); },
        error: function(err) {
            alert('通知失败: ' + ((err.responseJSON && err.responseJSON.detail) || '未知错误'));
        }
    });
}

function formatDateTime(dateString) {
    return RainbowBusinessTime.format(dateString);
}

function matchesOrderView(order, view) {
    if (!view || view === 'all') return true;
    if (view === 'split') return order.status === 'split_pending';
    if (view === 'pending') return order.status === 'pending' && !order.recorded;
    if (view === 'recorded') {
        return !!order.recorded && ['split_pending', 'shipped', 'cancelled'].indexOf(order.status) === -1;
    }
    return order.status === view;
}

function groupOrderItems(items) {
    var groups = {};
    (items || []).forEach(function(item) {
        var category = item.category_name || item.parent_name || '默认分类';
        var flavor = item.product_name || item.name || '-';
        var key = String(category);
        if (!groups[key]) {
            groups[key] = { category_name: category, quantity: 0, flavors: {} };
        }
        if (!groups[key].flavors[flavor]) {
            groups[key].flavors[flavor] = { product_name: flavor, quantity: 0, unit:item.unit||'支' };
        }
        var quantity = Number(item.quantity || 0);
        groups[key].quantity += quantity;
        groups[key].flavors[flavor].quantity += quantity;
    });
    return Object.keys(groups).map(function(key) {
        var group = groups[key];
        group.flavors = Object.keys(group.flavors).map(function(flavor) {
            return group.flavors[flavor];
        });
        return group;
    });
}


function deleteOrder(orderId, orderNumber, status) {
    var shipped = status === 'shipped';
    var msg = '确定删除订单 #' + (orderNumber || orderId) + ' 吗？\n\n删除后订单列表不再显示；如果订单未取消，系统会先自动取消并回滚库存。';
    if (shipped) {
        msg += '\n\n注意：该订单已发货，确认删除仍会回滚库存，请确认这不是正常已发货订单。';
    }
    if (!confirm(msg)) return;
    $.ajax({
        url: `/api/orders/${orderId}/delete`,
        method: 'POST',
        data: {
            reason: '后台订单管理删除',
            allow_shipped: shipped ? 'true' : 'false'
        },
        success: function(resp) {
            alert((resp && resp.message) || '订单已删除');
            loadOrders(true);
        },
        error: function(err) {
            alert('删除失败: ' + ((err.responseJSON && (err.responseJSON.detail || err.responseJSON.message)) || '未知错误'));
            loadOrders(true);
        }
    });
}

function toggleAllOrders(checked) {
    if(orderBatchBusy)return;
    invalidateOrderSelection();
    allOrders.forEach(o=>checked?orderSelection.add(String(o.id)):orderSelection.delete(String(o.id)));updateOrderSelection();
}

function selectedOrderIds() {
    return Array.from(orderSelection);
}

var orderBatchBusy=false;
function postSelectedOrderBatches(url,data,ids) {
    const deferred=$.Deferred(), frozen=Array.from(new Set(ids.map(String)));
    if(orderBatchBusy){deferred.reject({responseJSON:{detail:'上一批操作尚未结束，请勿重复提交'}});return deferred.promise();}
    orderBatchBusy=true;
    let offset=0,results=[];
    function next(){
        if(offset>=frozen.length){
            orderBatchBusy=false;
            const ok=results.filter(r=>r.success).length;
            deferred.resolve({results,total:frozen.length,updated:ok,message:'批量处理完成：'+ok+'/'+frozen.length+' 成功'});return;
        }
        const chunk=frozen.slice(offset,offset+100);
        $('#orderSelectionCount').text('正在处理 '+offset+'/'+frozen.length+' 单，请勿重复提交');
        $.post(url,Object.assign({},data,{order_ids:chunk.join(',')})).done(function(r){
            const rows=(r&&r.results)||[];
            const returned=new Set(rows.map(row=>String(row.order_id)));
            if(rows.length!==chunk.length || returned.size!==chunk.length || chunk.some(id=>!returned.has(id))){
                orderBatchBusy=false;deferred.reject({responseJSON:{detail:'返回结果数量不完整，已停止后续批次。请核对当前批次，禁止直接重试。'}});return;
            }
            results=results.concat(rows);offset+=chunk.length;next();
        }).fail(function(xhr){
            orderBatchBusy=false;
            deferred.reject({responseJSON:{detail:'已完成 '+offset+' 笔；当前批次结果可能未知，后续未发送。请核对后再操作。'+((xhr.responseJSON&&xhr.responseJSON.detail)||'')}});
        });
    }
    next();return deferred.promise();
}

function batchUpdateOrders(status, reason, confirmText) {
    var ids = selectedOrderIds();
    if (!ids.length) {
        alert('请先勾选订单');
        return;
    }
    if (!confirm(confirmText.replace('{n}', ids.length))) return;
    ensureOrderSelection().done(function(token){postSelectedOrderBatches('/api/orders/batch_update_status', {
        order_ids: ids.join(','),
        status: status,
        reason: reason,
        selection_token:token
    },ids).done(function(resp) {
        var msg = (resp && resp.message) || ('批量处理完成：' + ids.length);
        var failed = ((resp && resp.results) || []).filter(function(item) { return !item.success; });
        if (failed.length) {
            msg += '\n失败：' + failed.map(function(item) {
                return (item.order_number || ('#' + item.order_id)) + ' ' + (item.message || '');
            }).join('；');
        }
        alert(msg);
        loadOrders();
    }).fail(function(xhr) {
        alert((xhr.responseJSON && xhr.responseJSON.detail) || '批量处理失败');
        loadOrders();
    });
    }).fail(function(xhr){alert((xhr.responseJSON&&xhr.responseJSON.detail)||'固定批次创建失败');});
}

function batchConfirmOrders() {
    batchUpdateOrders('confirmed', '后台批量确认', '确认选中的 {n} 个订单？确认后会通知客户。');
}

function batchShipOrders() {
    batchUpdateOrders('shipped', '后台批量发货', '确认将选中的 {n} 个订单标记为已发货？发货后会通知客户。');
}

function batchCancelOrders() {
    openOrderCancellation(selectedOrderIds());
}

function batchPrintOrders(template) {
    var ids = selectedOrderIds();
    if (!ids.length) {
        alert('请先勾选订单');
        return;
    }
    var tpl = template || 'shipping';
    window.open('/admin/orders/print?ids=' + encodeURIComponent(ids.join(',')) + '&template=' + encodeURIComponent(tpl), '_blank');
}


function restoreOrder(orderId) {
    if (!confirm('\u786e\u5b9a\u6062\u590d\u8fd9\u4e2a\u5df2\u53d6\u6d88\u8ba2\u5355\uff1f\u5c06\u91cd\u65b0\u6263\u5e93\u5b58\u5e76\u53d8\u56de\u5f85\u5904\u7406\u3002')) return;
    $.post('/api/orders/' + orderId + '/restore', {
        to_status: 'pending',
        reason: '\u540e\u53f0\u6062\u590d\u5df2\u53d6\u6d88\u8ba2\u5355'
    }).done(function(resp) {
        alert((resp && resp.message) || '\u8ba2\u5355\u5df2\u6062\u590d');
        loadOrders(true);
    }).fail(function(xhr) {
        alert((xhr.responseJSON && xhr.responseJSON.detail) || '\u6062\u590d\u5931\u8d25');
    });
}

function batchRestoreOrders() {
    var ids = selectedOrderIds();
    if (!ids.length) {
        alert('\u8bf7\u5148\u52fe\u9009\u5df2\u53d6\u6d88\u8ba2\u5355');
        return;
    }
    if (!confirm('\u786e\u5b9a\u6279\u91cf\u6062\u590d\u9009\u4e2d\u7684 {n} \u4e2a\u8ba2\u5355\uff1f\u5c06\u91cd\u65b0\u6263\u5e93\u5b58\u3002'.replace('{n}', ids.length))) return;
    postSelectedOrderBatches('/api/orders/batch_restore', {
        order_ids: ids.join(','),
        to_status: 'pending',
        reason: '\u540e\u53f0\u6279\u91cf\u6062\u590d'
    },ids).done(function(resp) {
        var msg = (resp && resp.message) || '\u6279\u91cf\u6062\u590d\u5b8c\u6210';
        var failed = ((resp && resp.results) || []).filter(function(item) { return !item.success; });
        if (failed.length) {
            msg += '\n\u5931\u8d25\uff1a' + failed.map(function(item) {
                return (item.order_number || ('#' + item.order_id)) + ' ' + (item.message || '');
            }).join('\uff1b');
        }
        alert(msg);
        loadOrders(true);
    }).fail(function(xhr) {
        alert((xhr.responseJSON && xhr.responseJSON.detail) || '\u6279\u91cf\u6062\u590d\u5931\u8d25');
        loadOrders(true);
    });
}

function showCancelledRecords() {
    $('#orderViewFilter').val('cancelled');
    $('#orderPayFilter').val('all');
    syncQuickChips();
    loadOrders(true);
    $.when(
        $.get('/api/orders/cancelled'),
        $.get('/api/orders/deleted_traces')
    ).done(function(cancelledResp, deletedResp) {
        var cancelled = (cancelledResp[0] && cancelledResp[0].orders) || [];
        var traces = (deletedResp[0] && deletedResp[0].traces) || [];
        var lines = ['\u4ecd\u53ef\u6062\u590d\u7684\u5df2\u53d6\u6d88\u8ba2\u5355 ' + cancelled.length + ' \u5355\uff1a'];
        cancelled.slice(0, 30).forEach(function(order) {
            lines.push((order.order_number || '') + ' / ' + (order.recipient_name || '') + ' / ' + (order.recipient_phone || '') + ' / \u00a5' + Number(order.grand_total || 0).toFixed(2));
        });
        if (cancelled.length > 30) lines.push('...');
        lines.push('');
        lines.push('\u5df2\u786c\u5220\u9664\u53ea\u5269\u5e93\u5b58\u6d41\u6c34\u7684\u8ba2\u5355 ' + traces.length + ' \u5355\uff1a');
        traces.slice(0, 20).forEach(function(item) {
            lines.push((item.order_number || '') + ' / \u6570\u91cf ' + (item.total_qty || 0) + ' / \u6700\u540e ' + (item.last_at || ''));
        });
        alert(lines.join('\n'));
    }).fail(function() {
        alert('\u5df2\u5207\u6362\u5230\u5df2\u53d6\u6d88\u5217\u8868\u3002\u52fe\u9009\u540e\u53ef\u70b9\u6279\u91cf\u6062\u590d\u3002');
    });
}

function escapeHtml(text) {
    return String(text == null ? '' : text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function receiptDeliveryBadge(receipt) {
    if (!receipt || receipt.status === 'NOT_APPLICABLE') return '';
    const labels = {
        UNKNOWN: '订单已创建 · 群回执结果未知',
        NOT_SENT: '订单已创建 · 群回执未发送',
        PENDING: '订单已创建 · 群回执待发送',
        PARTIAL: '订单已创建 · 群回执未完整确认',
        NO_EVIDENCE: '订单已创建 · 群回执待核查',
        UNAVAILABLE: '群回执状态暂不可查',
        SENT: '群回执接口已确认'
    };
    const text = labels[receipt.status] || labels.NO_EVIDENCE;
    const warning = ['UNKNOWN', 'NOT_SENT', 'PARTIAL', 'NO_EVIDENCE', 'UNAVAILABLE'].includes(receipt.status);
    return '<div class="sk-meta ' + (warning ? 'text-danger' : 'text-muted') + '" title="订单创建与群内回执是不同状态，接口确认不代表已读；请勿因回执未知重复下单。">' + text + '</div>';
}
