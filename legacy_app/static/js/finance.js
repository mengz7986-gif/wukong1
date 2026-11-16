// Finance page - StyleKit UI
var allFinanceUsers = [];
var filteredFinanceUserIds = [];
var batchBillBusy = false;
var selectedFinanceUserIds = new Set();
var billSelectionFilter = sessionStorage.getItem('rainbow.billSelectionFilter');
var billSelectionVersion = 0;
var preparedBillPreview = null;
var billProgressTimer = null;
function billPreviewText(value){
    return String(value).replace(/<a href="tg:\/\/user\?id=\d+">([^<]*)<\/a>/g,'$1')
        .replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&#x27;/g,"'").replace(/&amp;/g,'&');
}
try { JSON.parse(sessionStorage.getItem('rainbow.billSelection')||'[]').forEach(function(id){if(Number.isSafeInteger(id)&&id>0)selectedFinanceUserIds.add(id);}); } catch(e) { sessionStorage.removeItem('rainbow.billSelection'); }
function persistBillSelection(){
    sessionStorage.setItem('rainbow.billSelection',JSON.stringify(Array.from(selectedFinanceUserIds)));
    if(billSelectionFilter!==null)sessionStorage.setItem('rainbow.billSelectionFilter',billSelectionFilter);
    $('#billSelectedCount').text('已选 '+selectedFinanceUserIds.size+' 人');
    var all=filteredFinanceUserIds.length>0&&filteredFinanceUserIds.every(function(id){return selectedFinanceUserIds.has(id);});
    $('#selectAllBillUsers').prop('checked',all).prop('indeterminate',!all&&selectedFinanceUserIds.size>0);
    $('.bill-user-select').each(function(){$(this).prop('checked',selectedFinanceUserIds.has(Number(this.dataset.userId)));});
}
function invalidateBillPreview(){preparedBillPreview=null;bootstrap.Modal.getInstance(document.getElementById('billBatchPreviewModal'))?.hide();}
function toggleBillUser(id,checked){
    if(batchBillBusy){persistBillSelection();return;}
    id=Number(id);if(!filteredFinanceUserIds.includes(id))return;
    if(checked)selectedFinanceUserIds.add(id);else selectedFinanceUserIds.delete(id);
    billSelectionVersion++;invalidateBillPreview();persistBillSelection();
}
function selectAllFilteredBillUsers(){
    if(batchBillBusy)return;
    selectedFinanceUserIds=new Set(filteredFinanceUserIds);billSelectionVersion++;invalidateBillPreview();persistBillSelection();
    $('#billBatchStatus').text('已固定选中全部当前筛选结果 '+selectedFinanceUserIds.size+' 人；刷新或新增用户不会自动加入。');
}
function clearBillSelection(){
    if(batchBillBusy)return;
    selectedFinanceUserIds.clear();billSelectionVersion++;invalidateBillPreview();persistBillSelection();
}
function startBillProgressPoll(){
    if(billProgressTimer)clearInterval(billProgressTimer);
    billProgressTimer=setInterval(function(){if(!document.hidden)showBillBatchProgress();},5000);
}
function showBillBatchProgress(){
    var key=sessionStorage.getItem('rainbow.lastBillBatch');
    if(!key){alert('还没有本浏览器登记的批次。');return;}
    $.get('/api/payments/bill-batches/progress',{operation_key:key}).done(function(r){
        var counts=r.counts;
        if(!(counts.pending||counts.sending)&&billProgressTimer){clearInterval(billProgressTimer);billProgressTimer=null;}
        $('#billBatchStatus').text('固定 '+r.count+' 人；成功 '+(counts.sent||0)+'；待发送 '+(counts.pending||0)+'；发送中/需核对 '+(counts.sending||0)+'；失败 '+(counts.failed||0)+'；结果未知 '+(counts.unknown||0));
        $('#billBatchDetails').empty();
        r.targets.forEach(function(t){
            var row=$('<div>').text('用户 #'+t.user_id+'：'+t.status+(t.error?' · '+t.error:''));
            if(t.status==='failed' && /^(telegram_http_(400|401|403|404|422)|telegram_delivery_queue_full|telegram_retry_after_exhausted|telegram_reply_unavailable|BOT token missing|chat_id missing)$/.test(t.error||'')){
                $('<button>').addClass('btn btn-sm btn-outline-secondary').text('仅恢复未送达通知').on('click',function(){retryBatchBill(key,t.user_id);}).appendTo(row);
            }
            $('#billBatchDetails').append(row);
        });
    }).fail(function(xhr){$('#billBatchStatus').text(financeError(xhr));});
}
function retryBatchBill(key,user){
    var storage='rainbow.batchRetry.'+key+'.'+user;
    var saved=sessionStorage.getItem(storage), value=saved?JSON.parse(saved):null;
    if(!value){
        var reason=prompt('恢复缘由（仅恢复已证明未送达通知，不新增账务）：');
        if(!reason || !reason.trim() || reason.length>500)return;
        value={batch_key:key,user_id:user,reason:reason.trim(),operation_key:crypto.randomUUID()};
        sessionStorage.setItem(storage,JSON.stringify(value));
    }
    $.ajax({url:'/api/payments/bill-batches/retry',method:'POST',contentType:'application/json',headers:{'X-Finance-CSRF':financeCsrf},data:JSON.stringify(value)})
    .done(function(){sessionStorage.removeItem(storage);showBillBatchProgress();})
    .fail(function(xhr){$('#billBatchStatus').text(financeError(xhr)+'；保留恢复编号。');});
}
function previewSelectedBills(){
    if(batchBillBusy)return;
    var ids=Array.from(selectedFinanceUserIds).sort(function(a,b){return a-b;});
    if(!ids.length || ids.length>100){alert('请勾选1至100位用户，空选择不会推送；全选范围是全部当前筛选结果。');return;}
    var saved=sessionStorage.getItem('rainbow.billBatch');
    var operation=saved?JSON.parse(saved):{operation_key:crypto.randomUUID(),user_ids:ids};
    if(JSON.stringify(operation.user_ids.slice().sort(function(a,b){return a-b;}))!==JSON.stringify(ids)){
        $('#billBatchStatus').text('另有结果待核对的固定批次，不能用新名单覆盖。请核对进度或放弃尚未确认的原预览。');return;
    }
    var version=billSelectionVersion;
    sessionStorage.setItem('rainbow.billBatch',JSON.stringify(operation));
    batchBillBusy=true;
    $.ajax({url:'/api/payments/bill-batches/preview',method:'POST',contentType:'application/json',headers:{'X-Finance-CSRF':financeCsrf},data:JSON.stringify(operation)})
    .done(function(r){
        if(version!==billSelectionVersion){$('#billBatchStatus').text('筛选或勾选已变化，旧预览未确认发送；保留原编号，请核对或放弃旧预览。');return;}
        var returned=(r.targets||[]).map(function(t){return t.user_id;}).sort(function(a,b){return a-b;});
        if(r.operation_key!==operation.operation_key||r.count!==ids.length||JSON.stringify(returned)!==JSON.stringify(ids)){
            $('#billBatchStatus').text('服务返回的固定名单不一致，禁止确认发送，保留原编号核查。');return;
        }
        var time=RainbowBusinessTime.format(r.cutoff);
        preparedBillPreview={operation:operation,facts:r};
        $('#billBatchPreviewSummary').text('固定选中 '+r.count+' 人；截止 '+time+'（北京时间）。确认后才实际推送，后续新用户及新订单不会加入本批次。');
        var box=$('#billBatchPreviewTargets').empty();
        r.targets.forEach(function(t){
            var user=allFinanceUsers.find(function(u){return Number(u.id)===t.user_id;});
            $('<h6>').text((user?(user.display_name||user.telegram_name||user.username||'用户'):'用户')+' #'+t.user_id+' → 接单群 '+t.chat_id).appendTo(box);
            $('<pre>').addClass('text-wrap border rounded p-2').text(billPreviewText(t.text)).appendTo(box);
        });
        $('#billBatchConfirm').text('确认推送给已选 '+r.count+' 人').prop('disabled',false);
        bootstrap.Modal.getOrCreateInstance(document.getElementById('billBatchPreviewModal')).show();
    }).fail(function(xhr){$('#billBatchStatus').text(financeError(xhr)+'；未确认发送。');})
    .always(function(){batchBillBusy=false;});
}
function confirmSelectedBills(){
    if(batchBillBusy||!preparedBillPreview)return;
    var preview=preparedBillPreview,operation=preview.operation;
    batchBillBusy=true;$('#billBatchConfirm').prop('disabled',true);
    $.ajax({url:'/api/payments/bill-batches/confirm',method:'POST',contentType:'application/json',headers:{'X-Finance-CSRF':financeCsrf},data:JSON.stringify({operation_key:operation.operation_key,expected_count:preview.facts.count})})
    .done(function(r){
        if(r.state!=='queued'){$('#billBatchStatus').text('未确认队列状态，保留原批次编号。');return;}
        sessionStorage.removeItem('rainbow.billBatch');sessionStorage.setItem('rainbow.lastBillBatch',operation.operation_key);
        selectedFinanceUserIds.clear();billSelectionVersion++;invalidateBillPreview();persistBillSelection();
        $('#billBatchStatus').text('固定批次已进入实际发送队列；请查看逐用户送达进度。');showBillBatchProgress();startBillProgressPoll();
    }).fail(function(xhr){$('#billBatchStatus').text(financeError(xhr)+'；确认结果待核对，重试沿用原编号，不重复发送。');})
    .always(function(){batchBillBusy=false;$('#billBatchConfirm').prop('disabled',false);});
}
// Retained alias for an old open tab; it no longer sends the whole filtered list.
function previewFilteredBills(){return previewSelectedBills();}
function discardPreparedBillPreview(){
    if(batchBillBusy)return;
    var saved=sessionStorage.getItem('rainbow.billBatch');
    if(!saved){alert('没有未确认的预览。');return;}
    var operation=JSON.parse(saved);
    batchBillBusy=true;
    $.get('/api/payments/bill-batches/progress',{operation_key:operation.operation_key}).done(function(r){
        if(r.state!=='prepared'){
            sessionStorage.setItem('rainbow.lastBillBatch',operation.operation_key);
            $('#billBatchStatus').text('该批次已登记发送，不能放弃或另建重复批次。');
            showBillBatchProgress();return;
        }
        if(confirm('仅放弃本浏览器未确认的预览，不删除记录、不发送消息。之后可重新预览勾选名单。')){
            sessionStorage.removeItem('rainbow.billBatch');
            invalidateBillPreview();
            $('#billBatchStatus').text('未确认预览已放弃，可重新预览。');
        }
    }).fail(function(xhr){$('#billBatchStatus').text(financeError(xhr)+'；无法核实状态，保留原批次编号。');})
    .always(function(){batchBillBusy=false;});
}
var financeSort = 'debt_desc';
function setFinanceSort(field, direction) {
    financeSort = field + '_' + direction;
    $('.finance-sort').attr('aria-pressed', 'false').removeClass('text-primary');
    $('.finance-sort[data-sort="' + financeSort + '"]').attr('aria-pressed', 'true').addClass('text-primary');
    renderDebtUsers();
}
var currentUsdtRate = null;
var paymentOperation = null;
var balanceOperation = null;
var billOperations = {};
function financeError(xhr){
    var detail=xhr.responseJSON && xhr.responseJSON.detail;
    if(typeof detail==='string')return detail;
    if(Array.isArray(detail))return detail.map(function(e){return (e.loc||[]).filter(function(x){return x!=='body';}).join(' / ')+': '+(e.msg||'参数无效');}).join('；');
    return '收款请求失败，请刷新页面后重试（'+(xhr.status||'网络异常')+'）';
}
function pushBill(userId){
    if(billOperations[userId])return;
    var storage='rainbow.singleBill.'+userId;
    var key=sessionStorage.getItem(storage)||crypto.randomUUID();
    sessionStorage.setItem(storage,key);billOperations[userId]=key;
    $.ajax({url:'/api/payments/push-bill',method:'POST',headers:{'X-Finance-CSRF':financeCsrf},data:{user_id:userId,operation_key:key}}).done(function(r){
        if(r.success)sessionStorage.removeItem(storage);
        alert(r.success?'账单已推送至该用户接单群':'账单未确认送达（'+(r.notice_status||'待核对')+'），保留原编号；不要重新新增推送。');
    }).fail(function(xhr){alert(financeError(xhr));}).always(function(){delete billOperations[userId];});
}

$(document).ready(function() {
    refreshFinance();
    $('#debtSearch').on('input', renderDebtUsers);
    $('#debtFilter').on('change', renderDebtUsers);
    $('#debtSort').on('change', renderDebtUsers);
    $('#payMethod').on('change', handlePaymentMethodChange);
    $('#payAmount').on('input', updateUsdtPreview);
    $('#payUser').on('change', fillPaymentAmountFromDebt);
    $('#balanceAdjustMode, #balanceAdjustAmount').on('input change', updateBalanceAdjustHint);
    handlePaymentMethodChange();
});

function refreshFinance() {
    loadUsers();
    loadPayments();
}

function loadUsers() {
    $.get('/api/users/audit', function(users) {
        allFinanceUsers = users || [];
        renderDebtUsers();
    }).fail(function() {
        $('#debtBody').html('<tr><td colspan="6" class="text-center text-muted">加载失败</td></tr>');
    });
}

function renderDebtUsers() {
    var keyword = ($('#debtSearch').val() || '').trim().toLowerCase();
    var filter = $('#debtFilter').val() || 'all';
    var signature=JSON.stringify([keyword,filter]);
    if(billSelectionFilter!==null&&billSelectionFilter!==signature){
        selectedFinanceUserIds.clear();billSelectionVersion++;invalidateBillPreview();
        $('#billBatchStatus').text('筛选已变化，勾选名单已清空，避免误发；原已登记批次不变。');
    }
    billSelectionFilter=signature;
    var sort = financeSort;
    var sel = $('#payUser');
    var current = sel.val();
    sel.empty();
    var body = $('#debtBody');
    body.empty();

    var list = allFinanceUsers.filter(function(u) {
        var debt = Number(u.debt || 0);
        if (filter === 'debt' && debt <= 0) return false;
        if (filter === 'clear' && debt > 0) return false;
        if (!keyword) return true;
        var hay = [u.display_name, u.telegram_name, u.username, u.telegram_id, u.id].join(' ').toLowerCase();
        return hay.indexOf(keyword) !== -1;
    });
    if (/^(debt|balance)_(asc|desc)$/.test(sort)) {
        list.sort(function(a, b) {
            var field = sort.split('_')[0];
            var delta = Number(a[field] || 0) - Number(b[field] || 0);
            if (Math.abs(delta) < 0.0001) return Number(a.id || 0) - Number(b.id || 0);
            return sort.endsWith('_asc') ? delta : -delta;
        });
    }

    var totalDebt = 0;
    filteredFinanceUserIds=list.map(function(u){return Number(u.id);});
    list.forEach(function(u) { totalDebt += Number(u.debt || 0); });
    $('#debtTotalPill').text('欠款合计 ¥' + totalDebt.toFixed(2));
    $('#debtCountText').text(list.length + ' 人');

    if (!list.length) {
        $('#debtEmpty').show();
        $('#debtTableWrap').hide();
    } else {
        $('#debtEmpty').hide();
        $('#debtTableWrap').show();
    }

    list.forEach(function(u) {
        var name = u.display_name || u.telegram_name || u.username || u.telegram_id;
        var telegramMeta = [
            u.telegram_name ? '飞机名：' + u.telegram_name : '',
            u.username ? '@' + u.username : '',
            u.telegram_id ? 'ID: ' + u.telegram_id : '',
        ].filter(Boolean).join(' · ');
        var debt = Number(u.debt || 0);
        var balance = Number(u.balance || 0);
        var canUsePrepaid = debt > 0.009 && balance > 0.009;
        sel.append(`<option value="${u.id}">${escapeFinanceHtml(name)} / 欠¥${debt.toFixed(2)}</option>`);
        body.append(`
          <tr>
            <td><input class="form-check-input bill-user-select" type="checkbox" data-user-id="${Number(u.id)}" aria-label="选择用户 ${Number(u.id)} 推送账单" onchange="toggleBillUser(${Number(u.id)},this.checked)"></td>
            <td>
              <div class="sk-strong">${escapeFinanceHtml(name)}</div>
              <div class="sk-meta">${escapeFinanceHtml(telegramMeta)}</div>
            </td>
            <td class="sk-money-debt">¥${debt.toFixed(2)}</td>
            <td>¥${balance.toFixed(2)}</td>
            <td><span class="badge badge-secondary">${u.preferred_pay_method || 'cny'}</span></td>
            <td>
              <div class="sk-actions">
                <button class="btn btn-sm btn-primary" onclick="selectUser(${u.id}, ${debt})">收款</button>
                ${canUsePrepaid ? `<button class="btn btn-sm btn-outline-success" onclick="applyPrepaid(${u.id}, ${debt}, ${balance})">用预存抵扣</button>` : ''}
                <button class="btn btn-sm btn-outline-warning" onclick="adjustBalance(${u.id})">调整预存</button>
                <button class="btn btn-sm btn-outline-danger" onclick="openReceivable(${u.id})">增加用户欠款金额</button>
                <button class="btn btn-sm btn-outline-secondary" onclick="pushBill(${u.id})">推送账单</button>
              </div>
            </td>
          </tr>`);
    });

    if (current) sel.val(String(current));
    persistBillSelection();
}

function selectUser(id, debt) {
    $('#payUser').val(String(id));
    fillPaymentAmountFromDebt();
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function handlePaymentMethodChange() {
    var isUsdt = ($('#payMethod').val() || 'cny') === 'usdt';
    $('#payAmountLabel').text(isUsdt ? '数量（USDT）' : '金额（人民币）');
    $('#payAmount').attr('step', isUsdt ? '0.0001' : '0.01');
    if (isUsdt) {
        $('#usdtRatePreview').show().text('正在获取 USDT/CNY 实时汇率...');
        // Always refresh when switching to USDT so the preview is not based on
        // an earlier page-level cache entry.
        loadUsdtRate(true, true);
    } else {
        $('#usdtRatePreview').hide().text('');
        fillPaymentAmountFromDebt();
    }
}

function loadUsdtRate(refresh, fillAmount) {
    $.get('/api/payments/usdt-rate' + (refresh ? '?refresh=true' : ''))
        .done(function(resp) {
            currentUsdtRate = Number(resp.rate || 0);
            if (!currentUsdtRate) {
                $('#usdtRatePreview').show().addClass('text-danger').text('实时汇率无效，暂不能使用 USDT 收款。');
                return;
            }
            $('#usdtRatePreview').removeClass('text-danger').show();
            if (fillAmount) fillPaymentAmountFromDebt();
            updateUsdtPreview();
        })
        .fail(function(xhr) {
            currentUsdtRate = null;
            $('#usdtRatePreview').show().addClass('text-danger').text(
                (xhr.responseJSON && xhr.responseJSON.detail) || '实时汇率获取失败，暂不能使用 USDT 收款。'
            );
        });
}

function fillPaymentAmountFromDebt() {
    var user = getFinanceUser($('#payUser').val()) || {};
    var debt = Number(user.debt || 0);
    if (debt <= 0) return;
    if (($('#payMethod').val() || 'cny') === 'usdt') {
        if (currentUsdtRate) $('#payAmount').val((debt / currentUsdtRate).toFixed(4));
    } else {
        $('#payAmount').val(debt.toFixed(2));
    }
    updateUsdtPreview();
}

function updateUsdtPreview() {
    if (($('#payMethod').val() || 'cny') !== 'usdt' || !currentUsdtRate) return;
    var quantity = Number($('#payAmount').val() || 0);
    var cny = quantity * currentUsdtRate;
    $('#usdtRatePreview').removeClass('text-danger').show().text(
        '实时汇率：1 USDT = ¥' + currentUsdtRate.toFixed(4) +
        '；预计入账 ¥' + cny.toFixed(2) + '（提交时由服务端再校验）'
    );
}

function submitPayment() {
    var data = {
        user_id: $('#payUser').val(),
        amount: $('#payAmount').val(),
        method: $('#payMethod').val(),
        note: $('#payNote').val(),
        operator: 'admin'
    };
    var orderId = $('#payOrderId').val();
    if (orderId) data.order_id = orderId;
    if (!data.user_id) {
        alert('请选择用户');
        return;
    }
    if (!data.amount || Number(data.amount) <= 0) {
        alert('请输入收款金额');
        return;
    }
    var signature=JSON.stringify(data);
    if(!paymentOperation || paymentOperation.signature!==signature){
        paymentOperation={signature:signature,key:crypto.randomUUID()};
    }
    data.operation_key=paymentOperation.key;
    $.post('/api/payments/create', data).done(function(resp) {
        var r = resp.result || {};
        var notice = resp.payment_notice || r.payment_notice || {};
        var receivedText = r.method === 'usdt'
            ? Number(r.original_amount || 0).toFixed(4) + ' USDT × ¥' + Number(r.exchange_rate || 0).toFixed(4) + ' = ¥' + Number(r.amount || 0).toFixed(2)
            : '¥' + Number(r.amount || 0).toFixed(2);
        var noticeHtml = '';
        if (notice.attempted === true && notice.sent === true) {
            noticeHtml = '<br><span class="text-success">已发送到账单来源群</span>';
        } else if (notice.attempted === false) {
            noticeHtml = '<br><span class="text-muted">未找到来源群订单，未发送群账单</span>';
        } else if (notice.attempted === true && notice.sent === false) {
            noticeHtml = '<br><span class="text-danger">收款已完成，但账务通知发送失败：'
                + $('<div>').text(notice.detail || '未知错误').html() + '</span>';
        }
        $('#payResult').html(`
          <div class="alert alert-success mb-0">
            收款成功<br>
            入账金额 <strong>${receivedText}</strong><br>
            冲减订单 ${(r.affected_orders || []).length} 个<br>
            当前欠款 <strong>¥${Number(r.user_debt || 0).toFixed(2)}</strong>
            ${Number(r.prepaid_added || 0) > 0 ? '<br>超出欠款已转入预存 <strong>¥' + Number(r.prepaid_added).toFixed(2) + '</strong>，当前预存 <strong>¥' + Number(r.user_balance || 0).toFixed(2) + '</strong>' : ''}
            ${noticeHtml}
          </div>`);
        $('#payAmount').val('');
        paymentOperation=null;
        $('#payNote').val('');
        $('#payOrderId').val('');
        refreshFinance();
    }).fail(function(xhr) {
        alert(financeError(xhr));
    });
}

var prepaidOperation = null;
var prepaidSelection = null;
function applyPrepaid(userId, debt, balance) {
    var amount = Math.min(Number(debt || 0), Number(balance || 0));
    if (!userId || amount <= 0) {
        alert('没有可抵扣的预存余额');
        return;
    }
    prepaidSelection = {userId:userId, amount:amount};
    $('#prepaidConfirmDescription').text('确认使用预存余额抵扣欠款 ¥' + amount.toFixed(2) + '？');
    $('#prepaidConfirmError').text('');
    $('#prepaidConfirmModal').modal('show');
}
function submitPrepaid() {
    if(!prepaidSelection)return;
    var userId=prepaidSelection.userId,amount=prepaidSelection.amount;
    $('#prepaidConfirmSubmit').prop('disabled',true);
    var signature = String(userId) + ':' + amount.toFixed(2);
    if (!prepaidOperation || prepaidOperation.signature !== signature) {
        prepaidOperation = {signature:signature, key:crypto.randomUUID()};
    }
    $.post('/api/payments/apply_prepaid', {
        operation_key: prepaidOperation.key,
        user_id: userId,
        amount: amount.toFixed(2),
        note: '后台收账页使用预存余额抵扣',
        operator: 'admin'
    }).done(function(resp) {
        prepaidOperation = null;
        prepaidSelection = null;
        $('#prepaidConfirmModal').modal('hide');
        var r = resp.result || {};
        $('#payResult').html(`
          <div class="alert alert-success mb-0">
            预存抵扣成功<br>
            抵扣金额 <strong>¥${Number(r.amount || 0).toFixed(2)}</strong><br>
            当前欠款 <strong>¥${Number(r.user_debt || 0).toFixed(2)}</strong>，
            剩余预存 <strong>¥${Number(r.user_balance || 0).toFixed(2)}</strong>
          </div>`);
        refreshFinance();
    }).fail(function(xhr) {
        $('#prepaidConfirmError').text((xhr.responseJSON && xhr.responseJSON.detail) || '预存抵扣失败');
    }).always(function(){
        $('#prepaidConfirmSubmit').prop('disabled',false);
    });
}

$(function(){
    $('body').append($('<div>').attr({id:'prepaidConfirmModal',tabindex:-1}).addClass('modal fade').html('<div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5 class="modal-title">确认预存抵扣</h5><button class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><p id="prepaidConfirmDescription"></p><p id="prepaidConfirmError" class="text-danger" role="alert"></p></div><div class="modal-footer"><button class="btn btn-outline-secondary" data-bs-dismiss="modal">取消</button><button id="prepaidConfirmSubmit" class="btn btn-primary">确认抵扣</button></div></div></div>'));
    $('#prepaidConfirmSubmit').on('click',submitPrepaid);
});

function getFinanceUser(userId) {
    userId = Number(userId);
    return (allFinanceUsers || []).find(function(user) {
        return Number(user.id) === userId;
    });
}

function showBalanceAdjustStatus(type, message) {
    $('#balanceAdjustStatus')
        .removeClass('alert-success alert-danger alert-warning alert-info')
        .addClass('alert alert-' + type)
        .text(message)
        .show();
}

function updateBalanceAdjustHint() {
    var mode = $('#balanceAdjustMode').val() || 'reduce';
    var amount = Number($('#balanceAdjustAmount').val());
    var balance = Number($('#balanceAdjustCurrentBalance').data('balance') || 0);
    var labelMap = { add: '增加预存', reduce: '减少预存', set: '设为固定余额' };
    var hint = '';

    if (!isNaN(amount)) {
        if (mode === 'add') hint = '调整后预存约为 ¥' + (balance + amount).toFixed(2);
        if (mode === 'reduce') hint = '调整后预存约为 ¥' + (balance - amount).toFixed(2);
        if (mode === 'set') hint = '调整后预存将设为 ¥' + amount.toFixed(2);
    }

    $('#balanceAdjustActionLabel').text(labelMap[mode] || '调整预存');
    $('#balanceAdjustHint').text(hint || '请选择调整方式并填写金额。');
}

function adjustBalance(userId) {
    var user = getFinanceUser(userId) || {};
    var balance = Number(user.balance || 0);
        var name = user.display_name || user.telegram_name || user.username || user.telegram_id || ('用户 #' + userId);

    $('#balanceAdjustUserId').val(userId);
    $('#balanceAdjustUserName').text(name);
    $('#balanceAdjustTelegramId').text(user.telegram_id || '-');
    $('#balanceAdjustCurrentBalance').text('¥' + balance.toFixed(2)).data('balance', balance);
    $('#balanceAdjustMode').val('reduce');
    $('#balanceAdjustAmount').val('');
    $('#balanceAdjustNote').val('');
    $('#balanceAdjustStatus').hide().text('');
    updateBalanceAdjustHint();
    $('#balanceAdjustModal').modal('show');
    setTimeout(function() { $('#balanceAdjustAmount').trigger('focus'); }, 150);
}

function escapeFinanceHtml(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function submitBalanceAdjust() {
    var userId = $('#balanceAdjustUserId').val();
    var mode = $('#balanceAdjustMode').val();
    var amount = Number($('#balanceAdjustAmount').val());
    var note = String($('#balanceAdjustNote').val() || '').trim();
    var balance = Number($('#balanceAdjustCurrentBalance').data('balance') || 0);
    var labelMap = { add: '增加预存', reduce: '减少预存', set: '设为固定余额' };

    if (!userId) {
        showBalanceAdjustStatus('danger', '缺少用户信息，请刷新后重试。');
        return;
    }
    if (!mode || !labelMap[mode]) {
        showBalanceAdjustStatus('danger', '请选择正确的调整方式。');
        return;
    }
    if (isNaN(amount) || amount < 0 || ((mode === 'add' || mode === 'reduce') && amount <= 0)) {
        showBalanceAdjustStatus('danger', mode === 'set' ? '设为固定余额不能小于 0。' : '增加或减少金额必须大于 0。');
        return;
    }
    if (mode === 'reduce' && amount > balance + 0.0001) {
        showBalanceAdjustStatus('danger', '减少金额不能超过当前预存余额 ¥' + balance.toFixed(2) + '。');
        return;
    }
    if (!note) {
        showBalanceAdjustStatus('danger', '请填写调整原因或备注，方便后续对账。');
        return;
    }

    $('#submitBalanceAdjustBtn').prop('disabled', true).html('<i class="fas fa-spinner fa-spin me-1"></i>提交中');
    var balanceSignature=JSON.stringify([userId,mode,amount,note]);
    if(!balanceOperation || balanceOperation.signature!==balanceSignature){
        balanceOperation={signature:balanceSignature,key:crypto.randomUUID()};
    }
    $.post('/api/users/' + userId + '/balance_adjust', {
        operation_key:balanceOperation.key,
        mode: mode,
        amount: amount.toFixed(2),
        note: note,
        operator: 'admin'
    }).done(function(resp) {
        var r = resp.result || {};
        $('#payResult').html(`
          <div class="alert alert-success mb-0">
            预存余额调整成功<br>
            原余额 <strong>¥${Number(r.old_balance || 0).toFixed(2)}</strong>，
            新余额 <strong>¥${Number(r.new_balance || 0).toFixed(2)}</strong><br>
            本次变动 <strong>¥${Number(r.delta || 0).toFixed(2)}</strong>
          </div>`);
        $('#balanceAdjustModal').modal('hide');
        balanceOperation=null;
        refreshFinance();
    }).fail(function(xhr) {
        showBalanceAdjustStatus('danger', (xhr.responseJSON && xhr.responseJSON.detail) || '预存余额调整失败');
    }).always(function() {
        $('#submitBalanceAdjustBtn').prop('disabled', false).html('<i class="fas fa-check me-1"></i>确认调整');
    });
}

var paymentHistory = {search:'',offset:0,limit:30,total:0,request:0};
function paymentHistoryEscape(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});
}
function searchPaymentHistory() {paymentHistory.search=$('#paymentHistorySearch').val().trim();paymentHistory.offset=0;loadPayments();}
function clearPaymentHistorySearch() {$('#paymentHistorySearch').val('');paymentHistory.search='';paymentHistory.offset=0;loadPayments();}
function paymentHistoryPage(direction) {
    var next=paymentHistory.offset+direction*paymentHistory.limit;
    if(next<0 || next>=paymentHistory.total)return;
    paymentHistory.offset=next;loadPayments();
}
function loadPayments() {
    var request=++paymentHistory.request;
    $('#paymentHistoryStatus').text('正在查询收款记录…');
    $('#paymentHistoryPrev,#paymentHistoryNext').prop('disabled',true);
    $.get('/api/payments',{limit:paymentHistory.limit,offset:paymentHistory.offset,search:paymentHistory.search,paginated:true}, function(response) {
        if(request!==paymentHistory.request)return;
        var rows=response.items || [];
        paymentHistory.total=Number(response.total)||0;
        if(paymentHistory.total===0)paymentHistory.offset=0;
        if(!rows.length && paymentHistory.offset>0 && paymentHistory.total>0){
            paymentHistory.offset=Math.floor((paymentHistory.total-1)/paymentHistory.limit)*paymentHistory.limit;loadPayments();return;
        }
        $('#paymentHistoryStatus').text('共 '+paymentHistory.total+' 条收款记录');
        $('#paymentHistoryPageLabel').text('第 '+(paymentHistory.total?Math.floor(paymentHistory.offset/paymentHistory.limit)+1:0)+' / '+Math.ceil(paymentHistory.total/paymentHistory.limit)+' 页');
        $('#paymentHistoryPrev').prop('disabled',paymentHistory.offset===0);
        $('#paymentHistoryNext').prop('disabled',paymentHistory.offset+paymentHistory.limit>=paymentHistory.total);
        if (!rows || !rows.length) {
            $('#paymentEmpty .sk-empty-title').text(paymentHistory.search?'没有匹配的收款记录':'还没有收款记录');
            $('#paymentEmpty .sk-empty-desc').text(paymentHistory.search?'请检查昵称、用户名或 Telegram ID，或清空搜索':'确认收款后会显示在这里');
            $('#paymentEmpty').show();
            $('#paymentList').hide().empty();
            return;
        }
        $('#paymentEmpty').hide();
        var html = `
          <table class="table table-hover sk-table mb-0">
            <thead>
              <tr>
                <th>时间</th>
                <th>用户</th>
                <th>金额</th>
                <th>方式</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>`;
        rows.forEach(function(p) {
            var methodText = p.method === 'prepaid' ? '预存抵扣' : p.method;
            var amountClass = p.method === 'prepaid' ? 'sk-money-debt' : 'sk-money-ok';
            var exchangeText = p.method === 'usdt' && p.original_amount != null && p.exchange_rate != null
                ? '<div class="sk-meta">' + Number(p.original_amount).toFixed(4) + ' USDT × ¥' + Number(p.exchange_rate).toFixed(4) + '</div>'
                : '';
            html += `
              <tr>
                <td class="sk-meta">${RainbowBusinessTime.format(p.created_at)}</td>
                <td>${paymentHistoryEscape(p.display_name || p.telegram_name || p.username || p.user_telegram_id || p.user_id)}<div class="sk-meta">${paymentHistoryEscape(p.username ? '@'+p.username : '')} · ${paymentHistoryEscape(p.user_telegram_id || '')}</div></td>
                <td class="${amountClass}">¥${Number(p.amount).toFixed(2)}${exchangeText}</td>
                <td><span class="badge badge-secondary">${paymentHistoryEscape(methodText)}</span></td>
                <td class="sk-meta">${paymentHistoryEscape(p.note || '-')}</td>
              </tr>`;
        });
        html += '</tbody></table>';
        $('#paymentList').show().html(html);
    }).fail(function(){
        if(request!==paymentHistory.request)return;
        $('#paymentHistoryStatus').text('收款记录查询失败，请重试');
        $('#paymentList').hide().empty();$('#paymentEmpty').hide();
    });
}
