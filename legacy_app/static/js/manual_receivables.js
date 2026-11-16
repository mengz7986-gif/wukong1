// Separate from receipts/prepaid. Keep the nonce across an uncertain response.
var receivableBusy = false;
var receivableOperation = null;
function openReceivable(userId) {
    var saved = sessionStorage.getItem('rainbow.pendingReceivable');
    if (saved) {
        receivableOperation = JSON.parse(saved);
        if (Number(receivableOperation.user) !== Number(userId)) {
            alert('上一笔新增待付请求结果待核对，请先为原用户重试或查看应收记录。'); return;
        }
    } else {
        receivableOperation = {user:userId, key:crypto.randomUUID()};
    }
    if (!document.getElementById('receivableModal')) {
        $('body').append('<div class="modal fade" id="receivableModal" tabindex="-1" data-bs-backdrop="static"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5 class="modal-title">新增待付金额（增加欠款）</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><p>这不是收款或调整预存，不生成商品订单、不改库存。收款时先抵订单欠款，再抵本项待付。</p><label class="form-label">金额（人民币，最多两位小数）</label><input id="receivableAmount" class="form-control" type="number" min="0.01" step="0.01"><label class="form-label mt-2">款项缘由（必填）</label><textarea id="receivableReason" class="form-control" maxlength="500"></textarea><div id="receivableStatus" class="mt-2" role="status"></div><div id="receivableHistory" class="small mt-3"></div></div><div class="modal-footer"><button class="btn btn-outline-secondary" data-bs-dismiss="modal">关闭</button><button id="receivableSubmit" class="btn btn-danger" onclick="submitReceivable()">确认增加欠款</button></div></div></div></div>');
    }
    $('#receivableAmount').val(receivableOperation.amount || '').prop('disabled', !!saved);
    $('#receivableReason').val(receivableOperation.reason || '').prop('disabled', !!saved);
    $('#receivableStatus').text(saved ? '上次请求结果待核对：重试使用同一编号，不会重复记账。' : '');
    $('#receivableSubmit').prop('disabled',false);
    $.get('/api/receivables',{user_id:userId,limit:10},function(rows){
        $('#receivableHistory').html('<strong>近期独立应收及通知状态</strong>'+rows.map(function(r){
            var recoverable=r.notification_status==='failed' && /^(telegram_http_(400|401|403|404|422)|telegram_delivery_queue_full|telegram_retry_after_exhausted|telegram_reply_unavailable|BOT token missing|chat_id missing)$/.test(r.notification_error||'');
            return '<div>#'+Number(r.id)+' · ¥'+Number(r.amount).toFixed(2)+' · 已结¥'+Number(r.paid).toFixed(2)+' · '+escapeFinanceHtml(r.reason)+' · 通知：'+escapeFinanceHtml(r.notification_status||'未知')+(recoverable?' <button class="btn btn-sm btn-outline-secondary" onclick="retryReceivableNotice('+Number(r.id)+')">仅恢复未送达通知</button>':'')+'</div>';
        }).join(''));
    });
    bootstrap.Modal.getOrCreateInstance(document.getElementById('receivableModal')).show();
}
function retryReceivableNotice(identifier) {
    if (receivableBusy) return;
    var saved=sessionStorage.getItem('rainbow.pendingReceivableNotice');
    var operation=saved ? JSON.parse(saved) : {id:identifier,key:crypto.randomUUID()};
    if (operation.id!==identifier) { alert('另一笔通知恢复结果待核对，请先处理原通知。'); return; }
    if (!saved) {
        operation.reason=prompt('请输入恢复缘由。仅恢复已证明未送达的通知，不新增欠款或收款：');
        if (!operation.reason || !operation.reason.trim() || operation.reason.length>500) return;
        operation.reason=operation.reason.trim();
        if (!confirm('确认只恢复这笔通知？接收地址保持原值，结果未知的通知不能重发。')) return;
    }
    sessionStorage.setItem('rainbow.pendingReceivableNotice',JSON.stringify(operation));
    receivableBusy=true;
    $.ajax({url:'/api/receivables/'+identifier+'/retry-notice',method:'POST',headers:{'X-Finance-CSRF':financeCsrf},data:{reason:operation.reason,operation_key:operation.key}})
    .done(function(){sessionStorage.removeItem('rainbow.pendingReceivableNotice');$('#receivableStatus').text('恢复已登记，等待通知发送；没有新增账务。');})
    .fail(function(xhr){$('#receivableStatus').text(financeError(xhr)+'：保留原恢复编号，勿另建请求。');})
    .always(function(){receivableBusy=false;});
}
function submitReceivable() {
    if (receivableBusy || !receivableOperation) return;
    var amount=$('#receivableAmount').val(), reason=($('#receivableReason').val()||'').trim();
    if (!/^\d+(?:\.\d{1,2})?$/.test(amount) || Number(amount)<=0 || Number(amount)>10000000 || !reason || reason.length>500) {
        $('#receivableStatus').text('请输入正数金额（最多两位小数）和款项缘由。'); return;
    }
    if (!receivableOperation.amount && !confirm('确认给该用户增加待付欠款 ¥'+Number(amount).toFixed(2)+'？这不是登记收款。')) return;
    receivableOperation.amount=amount;receivableOperation.reason=reason;
    sessionStorage.setItem('rainbow.pendingReceivable',JSON.stringify(receivableOperation));
    receivableBusy=true;
    $('#receivableSubmit,#receivableAmount,#receivableReason').prop('disabled',true);
    $.ajax({url:'/api/receivables/create',method:'POST',headers:{'X-Finance-CSRF':financeCsrf},
        data:{user_id:receivableOperation.user,amount:amount,reason:reason,operation_key:receivableOperation.key}})
    .done(function(r){
        sessionStorage.removeItem('rainbow.pendingReceivable');
        $('#receivableStatus').text('已保存'+(r.result.duplicate?'（原请求已处理，未重复记账）':'')+'，当前待付 ¥'+Number(r.result.user_debt).toFixed(2)+'。通知状态请查看应收记录。');
        receivableOperation=null;refreshFinance();
    }).fail(function(xhr){
        // A 401/403 on a retry says nothing about the earlier request. Keep
        // identity and inputs across every failed response, including logout.
        $('#receivableStatus').text(financeError(xhr)+'：原请求结果待核对，请原编号重试，勿重新新增。');
        $('#receivableSubmit').prop('disabled',false);
    }).always(function(){receivableBusy=false;});
}
