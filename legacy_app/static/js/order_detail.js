// 订单详情页面的JavaScript

$(document).ready(function() {
    // 从URL获取订单ID
    const urlParams = new URLSearchParams(window.location.search);
    const orderId = urlParams.get('id');

    if (orderId) {
        loadOrderDetail(orderId);
    } else {
        $('#order-content').html('<div class="alert alert-danger">订单ID不存在</div>');
        $('#loading').hide();
        $('#order-content').show();
    }
});

function loadOrderDetail(orderId) {
    $.ajax({
        url: `/api/orders/${orderId}`,
        method: 'GET',
        success: function(order) {
            displayOrderDetail(order);
            $('#loading').hide();
            $('#order-content').show();
        },
        error: function(err) {
            $('#order-content').html(`<div class="alert alert-danger">加载订单失败: ${err.responseJSON?.detail || '未知错误'}</div>`);
            $('#loading').hide();
            $('#order-content').show();
        }
    });
}

function displayOrderDetail(order) {
    const statusBadge = getStatusBadge(order.status);
    const createdAt = RainbowBusinessTime.format(order.created_at);

    let html = `
        <div class="row">
            <div class="col-md-6">
                <h4><i class="fas fa-info-circle"></i> 订单信息</h4>
                <table class="table table-sm">
                    <tr><th width="30%">订单号</th><td><strong>#${order.order_number}</strong></td></tr>
                    <tr><th>用户</th><td>${order.user_username || order.user_telegram_id}</td></tr>
                    <tr><th>状态</th><td>${statusBadge}</td></tr>
                    ${order.fhd_waybill_code ? `<tr><th>历史运单号</th><td>${order.fhd_cp_name || order.fhd_cp_code || ''} ${order.fhd_waybill_code}</td></tr>` : ''}
                    <tr><th>下单时间</th><td>${createdAt}</td></tr>
                    ${order.cancellation_reason ? `<tr><th>取消原因</th><td>${$('<div>').text(order.cancellation_reason).html()}</td></tr>` : ''}
                    ${order.notes ? `<tr><th>备注</th><td>${$('<div>').text(order.notes).html()}</td></tr>` : ''}
                </table>
            </div>
            <div class="col-md-6">
                <h4><i class="fas fa-truck"></i> 收货信息</h4>
                <table class="table table-sm">
                    <tr><th width="30%">收件人</th><td>${order.recipient_name}</td></tr>
                    <tr><th>电话</th><td>${order.recipient_phone}</td></tr>
                    <tr><th>地址</th><td>${order.recipient_address}</td></tr>
                </table>
            </div>
        </div>

        <hr>

        <div class="row">
            <div class="col-md-12">
                <h4><i class="fas fa-box"></i> 商品信息</h4>
    `;

    // 按仓库显示商品
    for (const [warehouseName, items] of Object.entries(order.items_by_warehouse)) {
        html += `
            <h5 class="mt-3">${warehouseName}</h5>
            <table class="table table-hover table-sm">
                <thead>
                    <tr>
                        <th>商品</th>
                        <th>单价</th>
                        <th>数量</th>
                        <th>小计</th>
                        <th>利润</th>
                    </tr>
                </thead>
                <tbody>
        `;

        let warehouseTotal = 0;
        let warehouseProfit = 0;
        items.forEach(item => {
            const subtotal = item.unit_price * item.quantity;
            const profit = item.profit || 0;
            warehouseTotal += subtotal;
            warehouseProfit += profit;
            html += `
                <tr>
                    <td>${item.category_name ? item.category_name + ' / ' : ''}${item.product_name}</td>
                    <td>¥${item.unit_price.toFixed(2)}</td>
                    <td>${item.quantity}${['支','颗','盒','瓶','包','套','件'].includes(item.unit)?item.unit:'支'}</td>
                    <td>¥${subtotal.toFixed(2)}</td>
                    <td class="text-success">${item.profit==null?'历史进货价缺失':'¥'+profit.toFixed(2)}</td>
                </tr>
            `;
        });

        // 获取该仓库的运费
        const warehouseId = order.items.find(item =>
            item.warehouse_name === warehouseName || (warehouseName === '默认仓库' && !item.warehouse_name)
        )?.warehouse_id;
        const shippingFee = warehouseId ? getShippingFeeForWarehouse(order.items, warehouseId) : 0;

        html += `
                </tbody>
                <tfoot>
                    <tr class="table-active">
                        <td colspan="3"><strong>${warehouseName} 商品合计</strong></td>
                        <td><strong>¥${warehouseTotal.toFixed(2)}</strong></td>
                        <td><strong class="text-success">${items.some(item=>item.profit==null)?'历史进货价缺失':'¥'+warehouseProfit.toFixed(2)}</strong></td>
                    </tr>
                    <tr class="table-info">
                        <td colspan="4"><strong>${warehouseName} 运费</strong></td>
                        <td><strong>¥${shippingFee.toFixed(2)}</strong></td>
                    </tr>
                </tfoot>
            </table>
        `;
    }

    html += `
            </div>
        </div>

        <hr>

        <div class="row">
            <div class="col-md-6 offset-md-6">
                <table class="table table-sm">
                    <tr><th width="60%">商品总计</th><td class="text-end">¥${order.total_amount.toFixed(2)}</td></tr>
                    <tr><th>运费总计</th><td class="text-end">¥${order.total_shipping_fee.toFixed(2)}</td></tr>
                    <tr class="table-success"><th><h4>订单总计</h4></th><td class="text-end"><h4>¥${order.grand_total.toFixed(2)}</h4></td></tr>
                    <tr class="table-info"><th>订单利润</th><td class="text-end"><strong class="text-success">${order.total_profit==null?'历史进货价缺失':'¥'+order.total_profit.toFixed(2)}</strong></td></tr>
                </table>
            </div>
        </div>
    `;

    $('#order-content').html(html);
    const receiptBox=$('<section>').addClass('border rounded p-3 my-3');
    const receiptButton=$('<button type="button">').addClass('btn btn-outline-primary btn-sm').text('只读查询本单回执证据');
    const receiptResult=$('<div>').addClass('small mt-2');
    receiptBox.append(receiptButton,receiptResult);$('#order-content').append(receiptBox);
    receiptButton.on('click',function(){
        receiptButton.prop('disabled',true);
        $.get('/api/orders/'+order.id+'/receipt-evidence').done(function(data){
            const labels={SENT:'订单已创建；回执接口已确认（非已读）',NOT_SENT:'订单已创建；回执明确未发送',UNKNOWN:'订单已创建；回执结果未知，不代表未发送，请勿重复下单',PENDING:'订单已创建；回执待发送',PARTIAL:'订单已创建；回执尚未完整确认',NO_EVIDENCE:'订单已创建；缺少回执证据，送达待核查'};
            receiptResult.empty().append($('<p>').text(labels[data.status]||'状态待核查'));
            receiptResult.append($('<p>').text('任务：'+data.tasks.map(t=>t.task_id+' / '+t.state).join('；')));
            data.direct_attempts.forEach(a=>receiptResult.append($('<div>').text('直接回执 '+a.attempt_id+' / '+(labels[a.status]||a.status)+' / Telegram ID '+(a.message_id||'无持久化返回'))));
            data.outbox.forEach(o=>receiptResult.append($('<div>').text('Outbox '+o.event_key+' / '+o.status+' / Telegram ID '+(o.delivered_message_id||'无持久化返回'))));
            receiptResult.append($('<p>').text('此操作不会发送、重发或修改历史状态。'));
        }).fail(function(){receiptResult.text('证据读取失败，未触发重发。');}).always(function(){receiptButton.prop('disabled',false);});
    });
    if ((order.bundle_sales||[]).length) {
        const box=$('<section>').addClass('border rounded p-3 my-3');
        box.append($('<h4>').text('福袋销售 / 实际履约快照'));
        order.bundle_sales.forEach(sale=>{
            const names={};sale.items.forEach(item=>{const u=['支','颗','盒','瓶','包','套','件'].includes(item.unit)?item.unit:'支';const key=JSON.stringify([item.product_name,u]);names[key]=(names[key]||0)+item.quantity;});
            box.append($('<div>').addClass('mb-2').text(sale.name+' ×'+sale.quantity+'份 · 所属分类 '+(sale.category_name||sale.category_id)+' · 成交¥'+Number(sale.amount).toFixed(2)+'；实际配货：'+Object.entries(names).map(([key,q])=>{const [n,u]=JSON.parse(key);return n+'×'+q+u;}).join('、')));
        });
        $('#order-content').prepend(box);
    }
    const allocation = order.allocation;
    const auditBox = $('<section>').addClass('border rounded p-3 mt-3').attr('id', 'allocation-audit');
    auditBox.append($('<h4>').text('保存的自动分仓依据'));
    if (!allocation) {
        auditBox.append($('<p>').text('该历史订单没有保存分仓快照，不按当前统计补造历史依据。'));
    } else {
        auditBox.append($('<p>').text('方式：' + allocation.allocation_mode + '；最终仓库：' + allocation.warehouse_name + ' (#' + allocation.warehouse_id + ')'));
        auditBox.append($('<p>').text('分配时间：' + allocation.assigned_at + '；统计日期：' + (allocation.audit.local_date || '-')));
        const weekly=allocation.audit.period==='CALENDAR_WEEK';
        if(weekly)auditBox.append($('<p>').text('北京时间本周范围：'+allocation.audit.local_week_start+' 至 '+allocation.audit.local_week_end+'（结束时刻不含）'));
        if(allocation.audit.period==='CALENDAR_DAY')auditBox.append($('<p>').text('北京时间当日范围：'+allocation.audit.local_day_start+' 至 '+allocation.audit.local_day_end+'（结束时刻不含）'));
        auditBox.append($('<p>').text('排序依据：' + JSON.stringify(allocation.audit.ranking)));
        const table = $('<table>').addClass('table table-sm');
        table.append($('<thead>').append($('<tr>').append($('<th>').text('候选仓库'), $('<th>').text(weekly?'本周原始订单数':'当日历史统计'), $('<th>').text('可用库存'))));
        const body = $('<tbody>');
        (allocation.candidate_summary || []).forEach(row => body.append($('<tr>').append(
            $('<td>').text(row.warehouse_name + ' (#' + row.warehouse_id + ')' + (row.warehouse_id === allocation.warehouse_id ? ' · 已选' : '')),
            $('<td>').text(weekly?row.weekly_quantity:(row.daily_quantity==null?'历史未保存':row.daily_quantity)), $('<td>').text(JSON.stringify(row.available_stock)))));
        table.append(body); auditBox.append(table);
    }
    $('#order-content').append(auditBox);
}

function getShippingFeeForWarehouse(items, warehouseId) {
    const item = items.find(item => item.warehouse_id === warehouseId);
    return item ? item.shipping_fee || 0 : 0;
}

function getStatusBadge(status) {
    const statusMap = {
        'pending': { text: '待处理', class: 'bg-warning' },
        'confirmed': { text: '已录单', class: 'bg-info' },
        'recorded': { text: '已录单', class: 'bg-info' },
        'shipped': { text: '已发货', class: 'bg-success' },
        'cancelled': { text: '已取消', class: 'bg-danger' }
    };

    const s = statusMap[status] || { text: status, class: 'bg-secondary' };
    return `<span class="badge ${s.class}">${s.text}</span>`;
}

// 复制订单信息函数
function copyOrderInfo() {
    const urlParams = new URLSearchParams(window.location.search);
    const orderId = urlParams.get('id');

    if (!orderId) {
        alert('订单ID不存在');
        return;
    }

    $.ajax({
        url: `/api/orders/${orderId}`,
        method: 'GET',
        success: function(order) {
            const formattedText = formatOrderForCopy(order);
            copyToClipboard(formattedText);
            showCopySuccess();
        },
        error: function(err) {
            alert('获取订单信息失败: ' + (err.responseJSON?.detail || '未知错误'));
        }
    });
}

// 格式化订单为文本格式
function formatOrderForCopy(order) {
    const createdAt = RainbowBusinessTime.format(order.created_at);

    let text = `📋 订单报单\n`;
    text += `━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n`;
    text += `订单号: #${order.order_number}\n`;
    text += `下单时间: ${createdAt}\n`;
    text += `订单状态: ${getStatusText(order.status)}\n\n`;
    (order.bundle_sales || []).forEach(sale => { text += `福袋：${sale.name} ×${sale.quantity}份\n`; });

    text += `📦 商品信息:\n`;
    text += `━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n`;

    let totalGoodsAmount = 0;
    for (const [warehouseName, items] of Object.entries(order.items_by_warehouse)) {
        if (warehouseName !== '默认仓库') {
            text += `\n【${warehouseName}】\n`;
        }

        items.forEach(item => {
            const subtotal = item.unit_price * item.quantity;
            totalGoodsAmount += subtotal;
            text += `• ${item.category_name ? item.category_name + ' / ' : ''}${item.product_name} x${item.quantity} = ¥${subtotal.toFixed(2)}\n`;
        });
    }

    text += `\n商品总计: ¥${totalGoodsAmount.toFixed(2)}\n\n`;

    text += `🚚 运费信息:\n`;
    text += `━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n`;

    if (order.total_shipping_fee > 0) {
        // 计算每个仓库的运费
        const warehouseFees = {};
        order.items.forEach(item => {
            if (item.warehouse_name) {
                warehouseFees[item.warehouse_name] = item.shipping_fee || 0;
            }
        });

        for (const [warehouse, fee] of Object.entries(warehouseFees)) {
            text += `• ${warehouse}: ¥${fee.toFixed(2)}\n`;
        }
        text += `\n运费总计: ¥${order.total_shipping_fee.toFixed(2)}\n`;
    } else {
        text += `免运费\n`;
    }

    text += `\n💰 订单总金额: ¥${order.grand_total.toFixed(2)}\n\n`;

    text += `🏠 收货信息:\n`;
    text += `━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n`;
    text += `收件人: ${order.recipient_name}\n`;
    text += `联系电话: ${order.recipient_phone}\n`;
    text += `收货地址: ${order.recipient_address}\n`;

    if (order.notes) {
        text += `\n📝 备注: ${order.notes}\n`;
    }

    return text;
}

function getStatusText(status) {
    const statusMap = {
        'pending': '待处理',
        'confirmed': '已录单',
        'recorded': '已录单',
        'shipped': '已发货',
        'cancelled': '已取消'
    };
    return statusMap[status] || status;
}

// 复制到剪贴板
function copyToClipboard(text) {
    if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(function() {
            console.log('复制成功');
        }).catch(function(err) {
            console.error('复制失败: ', err);
            fallbackCopyToClipboard(text);
        });
    } else {
        fallbackCopyToClipboard(text);
    }
}

// 兼容旧版浏览器的复制方法
function fallbackCopyToClipboard(text) {
    const textArea = document.createElement('textarea');
    textArea.value = text;
    textArea.style.top = '0';
    textArea.style.left = '0';
    textArea.style.position = 'fixed';
    textArea.style.opacity = '0';

    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();

    try {
        document.execCommand('copy');
        console.log('复制成功（兼容模式）');
    } catch (err) {
        console.error('复制失败: ', err);
        alert('复制失败，请手动复制');
    }

    document.body.removeChild(textArea);
}

// 显示复制成功提示
function showCopySuccess() {
    const toast = new bootstrap.Toast($('#copyToast')[0]);
    toast.show();
}


function ensureDetailActions(order){
  if(document.getElementById('detail-actions')) return;
  var box = document.createElement('div');
  box.id = 'detail-actions';
  box.className = 'mb-3';
  box.innerHTML = `
    <button class="btn btn-secondary btn-sm" onclick="window.open('/admin/orders/print?id=${order.id}&template=shipping','_blank')">打印发货单</button>
    <button class="btn btn-outline-secondary btn-sm" onclick="window.open('/admin/orders/print?id=${order.id}&template=statement','_blank')">打印对账单</button>
    <button class="btn btn-outline-info btn-sm" onclick="notifyFromDetail(${order.id}, 'shipped')">补发发货通知</button>
    <button class="btn btn-outline-primary btn-sm" onclick="notifyFromDetail(${order.id}, 'confirmed')">补发确认通知</button>
  `;
  var root = document.getElementById('order-detail') || document.querySelector('.container-fluid') || document.body;
  root.prepend(box);
}

function notifyFromDetail(orderId, event){
  if(!confirm('确认发送通知给客户？')) return;
  $.post(`/api/orders/${orderId}/notify`, {event: event})
    .done(function(resp){ alert(resp.message || '已发送'); })
    .fail(function(xhr){ alert((xhr.responseJSON && xhr.responseJSON.detail) || '发送失败'); });
}
