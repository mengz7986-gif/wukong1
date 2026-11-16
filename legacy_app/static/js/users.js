// Users management - StyleKit UI
var allUsers = [];
var allDropshipApplications = [];
var currentUserPriceResponse = null;
var userPriceDrafts = {};
var userPriceExpandedCategories = new Set();
var usersRequest = null;
var dropshipRequest = null;
var userPricesRequest = null;
var userRenderTimer = null;
var priceRenderTimer = null;

async function confirmedPriceFetch(url,options){
    let response=await fetch(url,options);
    if(response.status!==409)return response;
    const error=await response.clone().json(),detail=error.detail;
    if(!detail||detail.code!=='PRICE_CONFIRMATION_REQUIRED')return response;
    if(!confirm(detail.messages.join('\n')))throw Error('已取消，未修改价格。');
    const body=JSON.parse(options.body);
    body.price_review_token=detail.token;
    return fetch(url,{...options,body:JSON.stringify(body)});
}

async function resetUserPriceOverrides(){
    const userId=Number($('#userPriceUserId').val());
    if(!Number.isInteger(userId) || userId<=0) return;
    const button=$('#resetUserPrices');button.prop('disabled',true);
    try{
        const response=await fetch(`/api/users/${userId}/prices/reset-preview`,{cache:'no-store'});
        const preview=await response.json();
        if(!response.ok) throw new Error(preview.detail || '预览失败');
        if(!preview.count){alert('该用户没有专属价格覆盖');return;}
        if(!confirm(`清除该用户全部 ${preview.count} 项专属覆盖？以后按当前等级价计算；历史订单成交价不会改变。`)) return;
        const saved=await confirmedPriceFetch(`/api/users/${userId}/prices/reset`,{method:'POST',headers:{'Content-Type':'application/json','X-Price-CSRF':preview.csrf},body:JSON.stringify({token:preview.token,confirm:true})});
        const result=await saved.json();
        if(!saved.ok) throw new Error(result.detail || '恢复失败');
        userPriceDrafts={};
        alert(`已清除 ${result.deleted} 项覆盖。请重新打开价格窗口查看当前等级价。`);
        bootstrap.Modal.getInstance(document.getElementById('userPriceModal'))?.hide();
        refreshUsersPage();
    }catch(error){alert(error.message || '恢复失败，未确认结果前请刷新核查');}
    finally{button.prop('disabled',false);}
}

$(document).ready(function() {
    refreshUsersPage();
    $('#userSearch').on('input', scheduleUserRender);
    $('#userLevelFilter, #userDebtFilter, #userCustomPriceFilter').on('change', renderUsers);
    $('#resetUserPrices').on('click', resetUserPriceOverrides);
    $('#userPriceSearch').on('input', scheduleUserPriceRender);
    $('#userPriceBatchCategory').on('change', function(){renderUserPriceRows();});
});

function scheduleUserRender() {
    clearTimeout(userRenderTimer);
    userRenderTimer = setTimeout(renderUsers, 120);
}

function scheduleUserPriceRender() {
    clearTimeout(priceRenderTimer);
    priceRenderTimer = setTimeout(renderUserPriceRows, 120);
}

function refreshUsersPage() {
    loadUsers(true);
    loadDropshipApplications();
}

function loadUsers(force) {
    if (force) {
        $('#loading').show();
        $('#users-content').hide();
    }
    if (usersRequest) {
        usersRequest.abort();
    }
    var request = $.ajax({
        url: '/api/users/management',
        method: 'GET'
    });
    usersRequest = request;
    request.done(function(users) {
        if (usersRequest !== request) return;
        allUsers = users || [];
        renderUsers();
        $('#loading').hide();
        $('#users-content').show();
    }).fail(function(err, status) {
        if (status === 'abort' || usersRequest !== request) return;
        console.error('Failed to load users:', err);
        $('#loading').html('<div class="sk-empty"><div class="sk-empty-title">加载失败</div><div class="sk-empty-desc">请刷新重试</div></div>');
    }).always(function() {
        if (usersRequest === request) {
            usersRequest = null;
        }
    });
}

function loadDropshipApplications() {
    if (dropshipRequest) {
        dropshipRequest.abort();
    }
    var request = $.ajax({
        url: '/api/dropship/applications',
        method: 'GET',
        data: { status: 'pending' }
    });
    dropshipRequest = request;
    request.done(function(applications) {
        if (dropshipRequest !== request) return;
        allDropshipApplications = applications || [];
        renderDropshipApplications();
    }).fail(function(err, status) {
        if (status === 'abort' || dropshipRequest !== request) return;
        console.error('Failed to load dropship applications:', err);
        $('#dropshipEmpty').show().find('.sk-empty-title').text('代发申请加载失败');
        $('#dropshipTableWrap').hide();
    }).always(function() {
        if (dropshipRequest === request) {
            dropshipRequest = null;
        }
    });
}

function renderDropshipApplications() {
    var tbody = $('#dropship-tbody');
    if (!allDropshipApplications.length) {
        tbody.empty();
        $('#dropshipEmpty').show();
        $('#dropshipTableWrap').hide();
        return;
    }
    $('#dropshipEmpty').hide();
    $('#dropshipTableWrap').show();
    var rows = [];
    allDropshipApplications.forEach(function(app) {
        var display = app.user_display_name || app.telegram_name || app.username || app.telegram_id || '-';
        var safeName = (app.username || '').replace(/'/g, "\\'");
        var safeNote = escapeHtml(app.message || app.admin_note || '-');
        var telegramMeta = [
            app.telegram_name ? '飞机名：' + app.telegram_name : '',
            app.username ? '@' + app.username : '',
            app.telegram_id ? 'ID: ' + app.telegram_id : '',
        ].filter(Boolean).join(' · ');
        rows.push(`
        <tr>
          <td>
            <div class="sk-strong">${escapeHtml(display)}</div>
            <div class="sk-meta">${escapeHtml(telegramMeta)}</div>
          </td>
          <td>${getDropshipStatusBadge(app.status)}</td>
          <td class="sk-meta">${safeNote}</td>
          <td class="sk-meta">${formatDateTime(app.created_at)}</td>
          <td>
            <div class="sk-actions">
              <button class="btn btn-sm btn-info" onclick="showPriceModal(${app.user_id}, '${app.telegram_id}', '${safeName}')">设单价</button>
              <button class="btn btn-sm btn-success" onclick="updateDropshipApplication(${app.id}, 'approved')">通过</button>
              <button class="btn btn-sm btn-outline-danger" onclick="updateDropshipApplication(${app.id}, 'rejected')">拒绝</button>
            </div>
          </td>
        </tr>`);
    });
    tbody.html(rows.join(''));
}

function getDropshipStatusBadge(status) {
    if (status === 'approved') return '<span class="badge badge-success">已通过</span>';
    if (status === 'rejected') return '<span class="badge badge-danger">已拒绝</span>';
    return '<span class="badge badge-warning">待处理</span>';
}

function updateDropshipApplication(applicationId, status) {
    var text = status === 'approved' ? '通过该代发申请？确认前请先设置好该用户单价。' : '拒绝该代发申请？';
    if (!confirm(text)) return;
    $.ajax({
        url: `/api/dropship/applications/${applicationId}/status`,
        method: 'POST',
        data: { status: status },
        success: function() {
            loadDropshipApplications();
            loadUsers(true);
        },
        error: function(err) {
            alert('操作失败: ' + ((err.responseJSON && (err.responseJSON.detail || err.responseJSON.message)) || '未知错误'));
        }
    });
}

function renderUsers() {
    var keyword = ($('#userSearch').val() || '').trim().toLowerCase();
    var level = $('#userLevelFilter').val() || 'all';
    var debtFilter = $('#userDebtFilter').val() || 'all';
    var customFilter = $('#userCustomPriceFilter').val() || 'all';

    var list = allUsers.filter(function(user) {
        if(customFilter==='custom' && !Number(user.custom_price_count || 0)) return false;
        if(customFilter==='default' && Number(user.custom_price_count || 0)) return false;
        if (level !== 'all' && String(user.agent_level) !== String(level)) return false;
        var debt = Number(user.debt || 0);
        if (debtFilter === 'debt' && debt <= 0) return false;
        if (debtFilter === 'clear' && debt > 0) return false;
        if (!keyword) return true;
        var hay = [user.id, user.telegram_id, user.username, user.telegram_name, user.display_name, user.remark].join(' ').toLowerCase();
        return hay.indexOf(keyword) !== -1;
    });

    $('#userCountPill').text(list.length + ' 人');
    var tbody = $('#users-tbody');

    if (!list.length) {
        tbody.empty();
        $('#usersEmpty').show();
        $('#usersTableWrap').hide();
        return;
    }
    $('#usersEmpty').hide();
    $('#usersTableWrap').show();

    var rows = [];
    list.forEach(function(user) {
        var agentLevelBadge = getAgentLevelBadge(user.agent_level, user.agent_level_name);
        var statusBadge = user.is_active
            ? '<span class="badge badge-success">正常</span>'
            : '<span class="badge badge-danger">禁用</span>';
        const authLabels={none:'待授权',pending:'待审核',approved:'已授权',rejected:'授权已拒绝'};
        statusBadge+=' <span class="badge badge-warning">'+(authLabels[user.authorization_status]||'待授权')+'</span>';
        var display = user.telegram_name || 'Telegram 名称待获取';
        var debt = Number(user.debt || 0);
        var balance = Number(user.balance || 0);
        var safeName = (user.username || '').replace(/'/g, "\\'");
        var telegramName = user.display_name ? '后台备注：' + user.display_name : 'Telegram 显示名称';
        var telegramAccount = [
            user.username ? '@' + user.username : '',
            user.telegram_id ? 'ID: ' + user.telegram_id : '',
        ].filter(Boolean).join(' · ');
        rows.push(`
        <tr>
          <td class="sk-meta">${user.id}</td>
          <td>
            <div class="sk-strong">${escapeHtml(display)}</div>
            <div class="sk-meta">${escapeHtml(telegramName)} <button class="btn btn-sm btn-link" onclick="refreshUserProfile(${Number(user.id)})">同步资料</button></div>
            <div class="sk-meta">${escapeHtml(telegramAccount)}</div>
          </td>
          <td>${agentLevelBadge}<div class="sk-meta">专属覆盖：${Number(user.custom_price_count || 0)} 项</div></td>
          <td>
            <div class="sk-money-debt">欠 ¥${debt.toFixed(2)}</div>
            <div class="sk-meta">预存 ¥${balance.toFixed(2)}</div>
          </td>
          <td>${user.order_count || 0}</td>
          <td>${statusBadge}</td>
          <td class="sk-meta">${formatDate(user.created_at)}</td>
          <td>
            <div class="sk-actions">
              ${user.authorization_status!=='approved'?`<button class="btn btn-sm btn-outline-success" onclick="requestUserAuthorization(${user.id})">提交授权审核</button>`:''}
              <button class="btn btn-sm btn-warning" onclick="showAgentLevelModal(${user.id}, '${user.telegram_id}', '${safeName}', ${user.agent_level || 0})"><i class="fas fa-user-tag"></i> 等级</button>
              <button class="btn btn-sm btn-info" onclick="showPriceModal(${user.id}, '${user.telegram_id}', '${safeName}')">单价</button>
              <a class="btn btn-sm btn-outline-primary" href="/admin/finance">收账</a>
              <button class="btn btn-sm btn-outline-primary user-group-button" data-user="${user.id}">接单群</button>
              <button class="btn btn-sm btn-outline-danger" onclick="deleteUser(${user.id}, '${user.telegram_id}', '${safeName}', ${debt.toFixed(2)})"><i class="fas fa-trash"></i> 删除</button>
            </div>
          </td>
        </tr>`);
    });
    tbody.html(rows.join(''));
}

function getAgentLevelBadge(level, name) {
    var map = {
        0: 'badge-secondary',
        1: 'badge-primary',
        2: 'badge-info'
    };
    return `<span class="badge ${map[level] || 'badge-secondary'}">${name || getLevelName(level)}</span>`;
}

function showAgentLevelModal(userId, telegramId, username, currentLevel) {
    $('#agentLevelUserId').val(userId);
    $('#agentLevelSelect').val(currentLevel);
    $('#agentLevelUserInfo').val(`${telegramId} ${username ? '(' + username + ')' : ''} - 当前等级: ${getLevelName(currentLevel)}`);
    $('#agentLevelModal').modal('show');
}

function getLevelName(level) {
    switch (Number(level)) {
        case 1: return '超级VIP';
        case 2: return '合伙人';
        default: return 'VIP客户';
    }
}

function updateAgentLevel() {
    var userId = $('#agentLevelUserId').val();
    var level = $('#agentLevelSelect').val();
    $.ajax({
        url: `/api/users/${userId}/update_agent_level`,
        method: 'POST',
        data: { agent_level: level },
        success: function() {
            alert('客户等级已更新');
            $('#agentLevelModal').modal('hide');
            loadUsers(true);
        },
        error: function(err) {
            alert('设置失败: ' + ((err.responseJSON && (err.responseJSON.detail || err.responseJSON.message)) || '未知错误'));
        }
    });
}

function deleteUser(userId, telegramId, username, debt) {
    var label = username || telegramId || ('ID ' + userId);
    var message = '确定删除用户「' + label + '」吗？\n\n历史订单、收款记录和专属单价会保留，用户将从用户管理列表隐藏。';
    if (Number(debt || 0) > 0) {
        message += '\n\n注意：该用户当前仍有欠款 ¥' + Number(debt || 0).toFixed(2) + '。';
    }
    if (!confirm(message)) return;
    $.ajax({
        url: `/api/users/${userId}/delete`,
        method: 'POST',
        data: { reason: '后台用户管理删除' },
        success: function(resp) {
            alert((resp && resp.message) || '用户已删除');
            loadUsers(true);
            loadDropshipApplications();
        },
        error: function(err) {
            alert('删除失败: ' + ((err.responseJSON && (err.responseJSON.detail || err.responseJSON.message)) || '未知错误'));
        }
    });
}

function requestUserAuthorization(userId){
    const user=allUsers.find(row=>Number(row.id)===Number(userId));
    if(!user)return;
    if(!confirm('将此用户提交代发授权审核？不会自动开通下单；请先核对价格，再在申请区点击通过。'))return;
    $.ajax({url:'/api/dropship/apply',method:'POST',data:{telegram_id:user.telegram_id,username:user.username||'',message:'后台管理员提交授权审核'},
        success:function(){refreshUsersPage();alert('已提交，请在代发授权申请区设置单价并审核。');},
        error:function(err){alert((err.responseJSON||{}).detail||'提交失败，请刷新核对');}});
}

function refreshUserProfile(userId){
    $.ajax({url:'/api/users/'+userId+'/refresh-profile',method:'POST',
        success:function(){loadUsers(true);},
        error:function(err){const detail=(err.responseJSON||{}).detail;alert((detail&&detail.message)||detail||'资料读取失败，原资料未修改');}});
}

function addUser() {
    var telegramId = $('#telegramId').val().trim();
    if (!telegramId) {
        alert('请输入 Telegram ID');
        return;
    }
    $.ajax({
        url: '/api/users/admin-create',
        method: 'POST',
        data: { telegram_id: telegramId, authorize: $('#authorizeNewUser').prop('checked') },
        success: function(response) {
            alert(response.message+'；'+(response.authorized?'代发授权已开通。':'暂未开通代发授权。')+(response.profile_verified?'':' '+((response.profile_diagnostic||{}).message||'Telegram资料未获取，原资料未修改。')));
            $('#addUserModal').modal('hide');
            $('#addUserForm')[0].reset();
            loadUsers(true);
        },
        error: function(err) {
            alert('添加失败: ' + ((err.responseJSON && err.responseJSON.detail) || '未知错误'));
        }
    });
}

function escapeHtml(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatDate(dateString) {
    return RainbowBusinessTime.format(dateString);
}

function formatDateTime(dateString) {
    return RainbowBusinessTime.format(dateString);
}

var userCategoryPriceDrafts = {};
var categoryPriceSaving = false;
var categoryPriceRevision = 0;
var categoryPricePreviewSave = null;
function collectCategoryPriceChanges(ids,drafts,readValue){
    const changes=[];
    ids.forEach(id=>{
        let raw=Object.prototype.hasOwnProperty.call(drafts,String(id))?drafts[String(id)]:readValue(id);
        if(raw===null){changes.push({parent_id:id,unit_price:null,follow_all:true});return;}
        raw=String(raw==null?'':raw).trim();
        // Empty means unchanged, never zero and never deletion. A native
        // number input's badInput is retained separately by the input handler.
        if(raw==='')return;
        if(!/^\d+(?:\.\d{1,2})?$/.test(raw)||!Number.isFinite(Number(raw))||Number(raw)>1000000){
            const error=Error('请输入最多两位小数的有效分类价；删除请用“删除分类价”。');
            error.categoryId=id;throw error;
        }
        changes.push({parent_id:id,unit_price:Number(raw),follow_all:true});
    });
    return changes;
}
function setCategoryPriceDraft(id,value){
    if(value!==null&&String(value==null?'':value).trim()==='')delete userCategoryPriceDrafts[String(id)];
    else userCategoryPriceDrafts[String(id)]=value;
    categoryPriceRevision++;
    categoryPricePreviewSave=null;
    $('#categoryPriceImpactPanel').remove();categoryPriceSaving=false;
}
function categoryPriceControl(group,key){
    if(!/^\d+$/.test(key))return '';
    const all=((currentUserPriceResponse||{}).prices||[]).filter(p=>String(p.logical_category_id)===key);
    const values=[...new Set(all.map(p=>p.category_price==null?'unset':Number(p.category_price).toFixed(2)))];
    const current=values.length===1?(values[0]==='unset'?'未设置分类价':'¥'+values[0]):'跨仓分类价不一致，请明确指定';
    const actual=all.map(p=>Number(p.unit_price));
    const overrides=all.filter(p=>p.custom_price!=null).length;
    const hasDraft=Object.prototype.hasOwnProperty.call(userCategoryPriceDrafts,key);
    const value=hasDraft?userCategoryPriceDrafts[key]:(values.length===1&&values[0]!=='unset'?values[0]:'');
    return '<div class="category-price-control"><div class="category-price-editor">'+
      '<div><strong>当前价格：¥'+Math.min(...actual).toFixed(2)+'～¥'+Math.max(...actual).toFixed(2)+'</strong><div class="small text-muted">'+all.length+'个口味；修改分类后全部跟随（替换本分类原口味专属价；既有动态差价仍保留）。</div></div>'+
      '<label class="mb-0">修改分类价<input class="form-control form-control-sm category-price-input" aria-label="'+escapeHtml(group[0].parent_name)+'分类专属价" type="number" min="0" max="1000000" step="0.01" data-parent-id="'+key+'" value="'+escapeHtml(value==null?'':String(value))+'" placeholder="输入分类价格"></label>'+
      '<button type="button" class="btn btn-outline-primary btn-sm category-price-save" data-parent-id="'+key+'">预览</button>'+
      '<button type="button" class="btn btn-outline-secondary btn-sm category-price-delete" data-parent-id="'+key+'">删除分类价</button></div></div>';
}

function priceAwareAjax(options){
    const done=$.Deferred();
    const send=(settings,confirmed)=>$.ajax(settings).done((...args)=>done.resolve(...args)).fail(xhr=>{
        const detail=xhr.responseJSON&&xhr.responseJSON.detail;
        if(!confirmed&&xhr.status===409&&detail&&detail.code==='PRICE_CONFIRMATION_REQUIRED'){
            if(!window.confirm(detail.messages.join('\n')+'\n'+detail.message)){
                done.reject({responseJSON:{detail:'已取消，未修改价格。'}});return;
            }
            const next=Object.assign({},settings);
            if(settings.contentType==='application/json')next.data=JSON.stringify(Object.assign({},JSON.parse(settings.data),{price_review_token:detail.token}));
            else next.data=Object.assign({},settings.data,{price_review_token:detail.token});
            send(next,true);return;
        }
        if(detail&&detail.code==='PRICE_CONFIRMATION_REQUIRED')xhr.responseJSON.detail='成本、商品或价格已变化，请重新预览确认；未保存。';
        done.reject(xhr);
    });
    send(options,false);return done.promise();
}

async function previewCategoryFlavors(id){
    const key=String(id);
    const input=$('.category-price-input[data-parent-id="'+id+'"]');
    if(!Object.prototype.hasOwnProperty.call(userCategoryPriceDrafts,key)&&String(input.val()||'').trim()===''){
        $('#userPriceSaveStatus').removeClass('text-danger').text('请先填写这个分类的修改价格，未填写不会修改或删除原价格。');return;
    }
    if(!Object.prototype.hasOwnProperty.call(userCategoryPriceDrafts,key))setCategoryPriceDraft(id,input.val());
    userPriceExpandedCategories.add(key);
    renderUserPriceRows();
    const section=$('.category-price-input[data-parent-id="'+id+'"]').closest('section');
    section.find('.category-flavor-preview-status').remove();
    section.find('.category-price-control').after($('<div>').addClass('category-flavor-preview-status small text-muted my-2').text('正在计算各口味修改后价格，尚未保存…'));
    await saveCategoryPriceDrafts([Number(id)],key);
}

async function saveCategoryPriceDrafts(ids,flavorCategoryKey){
    if(categoryPriceSaving)return;
    ids=ids||Object.keys(userCategoryPriceDrafts).map(Number);
    if(!ids.length){$('#userPriceSaveStatus').text('尚未修改分类价格。');return;}
    const userId=String($('#userPriceUserId').val());
    let changes;
    try{changes=collectCategoryPriceChanges(ids,userCategoryPriceDrafts,id=>$('.category-price-input[data-parent-id="'+id+'"]').val());}catch(e){
      const invalid=$('.category-price-input[data-parent-id="'+e.categoryId+'"]');
      invalid.addClass('is-invalid');
      const label=invalid.attr('aria-label')||('分类 '+e.categoryId);
      $('#userPriceSaveStatus').addClass('text-danger').text(label+'：'+e.message);
      if(flavorCategoryKey!==undefined)$('.category-price-input[data-parent-id="'+flavorCategoryKey+'"]').closest('section').find('.category-flavor-preview-status').addClass('text-danger').text(e.message+'；未保存。');
      return;
    }
    if(!changes.length){$('#userPriceSaveStatus').removeClass('text-danger').text('未填写分类价格，原价格保持不变。请填写后预览；删除请用“删除分类价”。');return;}
    ids=changes.map(change=>change.parent_id);
    $('.category-price-input').removeClass('is-invalid');
    categoryPriceSaving=true;
    const revision=categoryPriceRevision;
    const request=async(url,body,csrf)=>{
      const response=await fetch(url,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Price-CSRF':csrf},body:JSON.stringify(body)});
      const data=await response.json();if(!response.ok)throw Error(data.detail||'分类价格操作失败');return data;
    };
    try{
      const auth=await fetch('/api/users/'+userId+'/prices/reset-preview',{credentials:'same-origin',cache:'no-store'});
      const meta=await auth.json();if(!auth.ok)throw Error(meta.detail||'会话验证失败');
      const preview=await request('/api/users/'+userId+'/prices/categories/preview',{categories:changes},meta.csrf);
      if(revision!==categoryPriceRevision||String($('#userPriceUserId').val())!==userId){categoryPriceSaving=false;return;}
      if(flavorCategoryKey!==undefined){
        const section=$('.category-price-input[data-parent-id="'+flavorCategoryKey+'"]').closest('section');
        const products=preview.categories.flatMap(c=>c.products);
        products.forEach(p=>{
          const row=section.find('[data-user-price-row="'+p.product_id+'"]');
          row.find('.user-price-input').val(Number(p.after).toFixed(2));
          row.find('.user-price-status').empty().append($('<span>').addClass('badge badge-warning').text('修改后 ¥'+Number(p.after).toFixed(2)+'（未保存）'));
          row.addClass('table-warning');
        });
        section.find('.category-flavor-preview-status').removeClass('text-muted text-danger').text('下方已展示各口味修改后价格，仅预览、未保存。请在底部预览分类修改后保存。');
        categoryPriceSaving=false;
        return;
      }
      $('#categoryPriceImpactPanel').remove();
      const panel=$('<section>').attr('id','categoryPriceImpactPanel').addClass('border rounded p-3 my-3');
      panel.append($('<h6>').text('价格修改预览（尚未保存）'));
      const table=$('<table>').addClass('table table-sm');
      table.append($('<thead>').append($('<tr>').append($('<th>').text('分类名称'),$('<th>').text('当前价格'),$('<th>').text('修改后价格'))));
      const body=$('<tbody>');const range=values=>{const lo=Math.min(...values),hi=Math.max(...values);return '¥'+lo.toFixed(2)+(lo===hi?'':'～¥'+hi.toFixed(2));};
      preview.categories.forEach(c=>{
        const sample=(currentUserPriceResponse.prices||[]).find(p=>Number(p.logical_category_id)===c.parent_id||c.parent_ids.includes(Number(p.parent_id)));
        body.append($('<tr>').append($('<td>').text(sample?.parent_name||'分类 '+c.parent_id),$('<td>').text(range(c.products.map(p=>p.before))),$('<td>').text(range(c.products.map(p=>p.after)))));
      });table.append(body);panel.append(table);
      const warningCounts=new Map();
      preview.categories.forEach(c=>{
        if(c.unit_price===0)panel.append($('<p>').addClass('text-warning').text('分类ID '+c.parent_id+'：当前售价为0元，是否确认保存？'));
        c.products.forEach(p=>{if(!p.retained_override)(p.sale_review?.warnings||[]).forEach(message=>warningCounts.set(message,(warningCounts.get(message)||0)+1));});
      });
      warningCounts.forEach((count,message)=>panel.append($('<p>').addClass('text-warning small').text(count+'个商品：'+message)));
      const error=$('<div>').addClass('text-danger small');
      const save=$('<button type="button">').addClass('btn btn-primary me-2').text('确认统一保存');
      const cancel=$('<button type="button">').addClass('btn btn-outline-secondary').text('取消');
      panel.append(error,cancel);$('#userPriceSaveStatus').before(panel);
      panel[0].scrollIntoView({block:'start',behavior:'smooth'});
      categoryPricePreviewSave=save;categoryPriceSaving=false;
      $('#userPriceSaveStatus').removeClass('text-danger').text('预览未保存。核对后点击底部“保存修改”；重新编辑需重新预览。');
      cancel.on('click',()=>{panel.remove();categoryPriceSaving=false;categoryPricePreviewSave=null;});
      save.on('click',async()=>{
        if(revision!==categoryPriceRevision||String($('#userPriceUserId').val())!==userId){error.text('当前用户或草稿已变化，请重新预览。');categoryPriceSaving=false;return;}
        if(categoryPriceSaving)return;categoryPriceSaving=true;save.prop('disabled',true);
        try{
          await request('/api/users/'+userId+'/prices/categories/save',{categories:changes,token:preview.token,confirm:true},meta.csrf);
          if(String($('#userPriceUserId').val())!==userId)return;
          ids.forEach(id=>delete userCategoryPriceDrafts[String(id)]);
          const refreshed=await fetch('/api/users/'+userId+'/prices',{credentials:'same-origin',cache:'no-store'});
          if(!refreshed.ok)throw Error('保存已完成，但刷新失败；请关闭后重新查看，不要重复提交。');
          if(String($('#userPriceUserId').val())!==userId)return;
          currentUserPriceResponse=await refreshed.json();panel.remove();renderUserPriceRows();
          categoryPricePreviewSave=null;
          $('#userPriceSaveStatus').removeClass('text-danger').text('已保存'+changes.length+'个分类，所选分类口味已统一跟随分类价，其他分类及历史订单不变。');
        }catch(e){error.text(e.message);/* A fresh preview is required; never retry writes automatically. */}
        finally{categoryPriceSaving=false;}
      });
    }catch(e){
      $('#userPriceSaveStatus').addClass('text-danger').text(e.message);
      if(flavorCategoryKey!==undefined)$('.category-price-input[data-parent-id="'+flavorCategoryKey+'"]').closest('section').find('.category-flavor-preview-status').addClass('text-danger').text('预览失败：'+e.message+'；未保存。');
      categoryPriceSaving=false;
    }
}

function showPriceModal(userId, telegramId, username) {
    categoryPricePreviewSave=null;
    categoryPriceRevision++;
    categoryPriceSaving=false;$('#categoryPriceImpactPanel').remove();
    userCategoryPriceDrafts = {};
    userPriceDrafts = {};
    userPriceExpandedCategories = new Set();
    if (userPricesRequest) {
        userPricesRequest.abort();
    }
    $('#userPriceUserId').val(userId);
    $('#userPriceUserName').text(username || telegramId || '-');
    $('#userPriceTelegramId').text(telegramId || '-');
    $('#userPriceDebt').text('¥0.00');
    $('#userPriceBalance').text('¥0.00');
    $('#userPriceSearch').val('');
    $('#userPriceBatchCategory').html('<option value="">正在加载商品品类...</option>');
    $('#userPriceBatchPrice').val('');
    $('#userPriceBatchButton').prop('disabled', true);
    $('#userPriceBatchHint').removeClass('text-danger text-success').addClass('text-muted').text('口味数按准确名称去重；商品数包含跨仓副本。提交才改价。');
    $('#user-price-tbody').empty();
    $('#userPriceTableWrap').hide();
    $('#userPriceEmpty').hide();
    $('#userPriceLoading').show();
    $('#userPriceSaveStatus').text('');
    $('#saveUserPricesButton').prop('disabled', true);
    $('#userPriceModal').modal('show');

    var request = $.get(`/api/users/${userId}/prices`);
    userPricesRequest = request;
    request
        .done(function(resp) {
            if (userPricesRequest !== request) return;
            currentUserPriceResponse = resp || {prices: []};
            $('#userPriceDebt').text('¥' + Number(resp.debt || 0).toFixed(2));
            $('#userPriceBalance').text('¥' + Number(resp.balance || 0).toFixed(2));
            buildUserPriceCategoryOptions();
            renderUserPriceRows();
            $('#saveUserPricesButton').prop('disabled', false);
            $('#userPriceBatchButton').prop('disabled', false);
        })
        .fail(function(xhr, status) {
            if (status === 'abort' || userPricesRequest !== request) return;
            currentUserPriceResponse = null;
            $('#userPriceLoading').hide();
            $('#userPriceEmpty').show()
                .find('.sk-empty-title').text('商品价格加载失败');
            $('#userPriceEmpty').find('.sk-empty-desc').text(
                (xhr.responseJSON && (xhr.responseJSON.detail || xhr.responseJSON.message)) || '请刷新后重试'
            );
        })
        .always(function() {
            if (userPricesRequest === request) {
                userPricesRequest = null;
            }
        });
}

function buildUserPriceCategoryOptions() {
    var items = ((currentUserPriceResponse && currentUserPriceResponse.logical_categories) || []).slice().sort(function(a, b) {
        return a.name.localeCompare(b.name, 'zh-CN');
    });
    var $select = $('#userPriceBatchCategory');
    $select.empty();
    if (!items.length) {
        $select.append('<option value="">暂无可批量设置的品类</option>');
        $('#userPriceBatchButton').prop('disabled', true);
        return;
    }
    $select.append('<option value="">选择商品品类</option>');
    items.forEach(function(item) {
        var label = item.name + '（' + item.flavor_count + ' 种口味 / ' + item.product_count + ' 个商品）';
        $select.append('<option value="' + item.id + '">' + escapeHtml(label) + '</option>');
    });
}

function renderUserPriceRows() {
    var response = currentUserPriceResponse || {};
    var prices = response.prices || [];
    var keyword = ($('#userPriceSearch').val() || '').trim().toLowerCase();
    var selectedCategory = $('#userPriceBatchCategory').val();
    var filtered = prices.filter(function(price) {
        if (selectedCategory && String(price.logical_category_id) !== String(selectedCategory)) return false;
        if (!keyword) return true;
        return [
            price.parent_name,
            price.product_name,
            price.category_display_id,
            price.product_display_id,
            price.warehouse_name,
            price.product_id
        ].join(' ').toLowerCase().indexOf(keyword) !== -1;
    });

    $('#userPriceLoading').hide();
    var tbody = $('#user-price-tbody');
    if (!filtered.length) {
        $('#categoryPriceEditors').empty();
        tbody.empty();
        $('#userPriceTableWrap').hide();
        $('#userPriceEmpty').show();
        return;
    }
    $('#userPriceEmpty').hide();
    $('#userPriceTableWrap').show();

    var rows = [];
    var editors = [];
    var grouped = new Map();
    filtered.forEach(function(price){
        var key=String(price.logical_category_id == null ? ('parent:'+price.parent_id) : price.logical_category_id);
        if(!grouped.has(key))grouped.set(key,[]);
        grouped.get(key).push(price);
    });
    grouped.forEach(function(group,key){
        const groupRowStart=rows.length;
        var expanded=userPriceExpandedCategories.has(key)||Boolean(keyword);
        editors.push('<section class="user-price-category border rounded p-2 mb-2"><button type="button" class="btn btn-sm btn-light user-price-category-toggle" data-category-key="'+escapeHtml(key)+'" aria-expanded="'+expanded+'">'+(expanded?'▾ ':'▸ ')+escapeHtml(group[0].parent_name||'默认分类')+' · '+group.length+' 个口味</button>'+categoryPriceControl(group,key)+'</section>');
        if(!expanded)return;
        group.forEach(function(price) {
        var effectivePrice = Number(price.unit_price || 0);
        var defaultPrice = Number(price.default_price == null ? effectivePrice : price.default_price);
        var customPrice = price.custom_price == null ? '' : Number(price.custom_price).toFixed(2);
        var category = price.parent_name || '默认分类';
        var categoryId = price.category_display_id || '';
        var productId = price.product_display_id || ('P' + String(price.product_id).padStart(3, '0'));
        var initialValue = customPrice === '' ? effectivePrice.toFixed(2) : customPrice;
        var draftValue = Object.prototype.hasOwnProperty.call(userCategoryPriceDrafts,key) && userCategoryPriceDrafts[key]!=null ? userCategoryPriceDrafts[key] : Object.prototype.hasOwnProperty.call(userPriceDrafts,price.product_id) ? userPriceDrafts[price.product_id] : initialValue;
        var status = price.is_custom
            ? '<span class="badge badge-primary">'+(price.price_source==='category'?'继承分类专属价':'口味专属价')+'</span><div class="small">与等级价差：¥'+(effectivePrice-defaultPrice).toFixed(2)+'</div>'
            : '<span class="badge badge-secondary">跟随默认价</span>';
        rows.push(`
          <tr class="${draftValue!==initialValue ? 'table-warning' : ''}" data-user-price-row="${price.product_id}" data-parent-id="${price.parent_id || ''}">
            <td>
              <div class="sk-strong">${escapeHtml(category)}</div>
              <div class="sk-meta">${escapeHtml(categoryId)}</div>
            </td>
            <td class="sk-strong">${escapeHtml(price.product_name || '-')}<div class="sk-meta">${escapeHtml(price.warehouse_name || '未指定仓库')}</div></td>
            <td class="sk-meta">${escapeHtml(productId)} <span class="text-muted">(${price.product_id})</span></td>
            <td>¥${defaultPrice.toFixed(2)}</td>
            <td class="user-price-effective">¥${effectivePrice.toFixed(2)}</td>
            <td>
              <input type="number" class="form-control form-control-sm user-price-input"
                     data-product-id="${price.product_id}"
                     data-is-custom="${price.is_custom ? 'true' : 'false'}"
                     data-initial-value="${initialValue}"
                     value="${escapeHtml(draftValue)}" min="0" step="0.01">
              <button type="button" class="btn btn-sm btn-outline-primary mt-1 canonical-price-unify" data-product-id="${price.product_id}">指定跨仓统一价</button>
              ${price.custom_price!=null?'<button type="button" class="btn btn-sm btn-outline-secondary mt-1 flavor-price-delete" data-product-id="'+price.product_id+'">删除此口味特殊价</button>':''}
            </td>
            <td class="user-price-status">${status}</td>
          </tr>`);
        });
        const header=$('#userPriceTableWrap table thead').html()||'';
        editors[editors.length-1]=editors[editors.length-1].replace(/<\/section>$/,'<div class="table-responsive mt-2"><table class="table table-sm"><thead>'+header+'</thead><tbody>'+rows.slice(groupRowStart).join('')+'</tbody></table></div></section>');
    });
    tbody.empty();
    $('#categoryPriceEditors').html(editors.join(''));
    $('#userPriceTableWrap').hide();
    const editor=$('#categoryPriceEditors');
    editor.find('.category-inheritance-review').on('click',function(){reviewCategoryInheritance(Number($(this).attr('data-parent-id')));});
    editor.find('.category-price-input').on('input',function(){
        const raw=$(this).val(),bad=this.validity&&this.validity.badInput;
        setCategoryPriceDraft($(this).attr('data-parent-id'),bad?'__INVALID_NUMBER__':raw);
        $(this).removeClass('is-invalid');
        const section=$(this).closest('section');
        if(!bad&&String(raw).trim()!=='')section.find('.user-price-input').val(raw);
        else section.find('.user-price-input').each(function(){
            const product=$(this).data('product-id');
            $(this).val(Object.prototype.hasOwnProperty.call(userPriceDrafts,product)?userPriceDrafts[product]:$(this).data('initial-value'));
        });
    });
    editor.find('.category-price-save').on('click',function(){previewCategoryFlavors(Number($(this).attr('data-parent-id')));});
    editor.find('.category-price-delete').on('click',function(){const id=Number($(this).attr('data-parent-id'));setCategoryPriceDraft(id,null);saveCategoryPriceDrafts([id]);});
    editor.find('.canonical-price-unify').on('click',function(){unifyCanonicalUserPrice(Number($(this).attr('data-product-id')));});
    editor.find('.flavor-price-delete').on('click',function(){deleteFlavorPrice(Number($(this).attr('data-product-id')));});
    editor.find('.user-price-category-toggle').on('click',function(){
        var key=String($(this).attr('data-category-key'));
        if(userPriceExpandedCategories.has(key))userPriceExpandedCategories.delete(key);
        else userPriceExpandedCategories.add(key);
        renderUserPriceRows();
    });

    $('.user-price-input').off('input').on('input', function() {
        var $input = $(this);
        var initialValue = Number($input.data('initial-value'));
        var currentValue = Number($input.val());
        userPriceDrafts[$input.data('product-id')] = $input.val();
        var changed = $input.val().trim() !== '' && !isNaN(currentValue) &&
            Math.abs(currentValue - initialValue) > 0.000001;
        var $row = $input.closest('tr');
        var isCustom = String($input.data('is-custom')) === 'true';
        $row.toggleClass('table-warning', changed);
        $row.find('.user-price-status').html(changed
            ? '<span class="badge badge-warning">待保存</span>'
            : (isCustom
                ? '<span class="badge badge-primary">已设专属价</span>'
                : '<span class="badge badge-secondary">跟随默认价</span>'));
    });
}

function batchSetUserPriceByCategory() {
    const categoryId=Number($('#userPriceBatchCategory').val());
    if(!categoryId){$('#userPriceBatchHint').text('请先选择商品分类');return;}
    setCategoryPriceDraft(categoryId,String($('#userPriceBatchPrice').val()||'').trim());
    saveCategoryPriceDrafts([categoryId]);
}

function legacyCategoryPriceFormUnused() {
    var userId = $('#userPriceUserId').val();
    var parentId = $('#userPriceBatchCategory').val();
    var raw = ($('#userPriceBatchPrice').val() || '').trim();
    var price = Number(raw);
    if (!parentId) {
        $('#userPriceBatchHint').removeClass('text-muted text-success').addClass('text-danger').text('请先选择商品品类');
        return;
    }
    if (!/^\d+(?:\.\d{1,2})?$/.test(raw) || !Number.isFinite(price) || price > 1000000) {
        $('#userPriceBatchPrice').addClass('is-invalid');
        $('#userPriceBatchHint').removeClass('text-muted text-success').addClass('text-danger').text('请输入有效单价');
        return;
    }
    $('#userPriceBatchPrice').removeClass('is-invalid');
    var selectedName = $('#userPriceBatchCategory option:selected').text();
    var group=((currentUserPriceResponse && currentUserPriceResponse.logical_categories)||[]).find(function(g){return String(g.id)===String(parentId);});
    if (!group || !confirm('将“'+selectedName+'”分类专属价设置为 ¥'+price.toFixed(2)+'？覆盖该分类的跨仓商品；已单独设置的口味价格优先，不修改其他分类或客户。')) return;
    var $button = $('#userPriceBatchButton');
    $button.prop('disabled', true).html('<span class="spinner-border spinner-border-sm me-1"></span>保存中');
    $('#userPriceBatchHint').removeClass('text-danger text-success').addClass('text-muted').text('正在同步 ' + selectedName + '...');
    priceAwareAjax({
        url: '/api/users/' + userId + '/prices',
        method: 'POST',
        data: {
            logical_category_id: parentId,
            expected_product_ids: JSON.stringify(group.product_ids),
            unit_price: price.toFixed(2)
        }
    }).done(function(resp) {
        var count = resp.updated_count || 0;
        $('#userPriceBatchHint').removeClass('text-muted text-danger').addClass('text-success').text('已同步 ' + count + ' 个商品');
        $button.prop('disabled', false).html('批量设置');
        showPriceModal(userId, $('#userPriceTelegramId').text(), $('#userPriceUserName').text());
    }).fail(function(xhr) {
        var message = (xhr.responseJSON && (xhr.responseJSON.detail || xhr.responseJSON.message)) || '批量设置失败，请重试';
        $('#userPriceBatchHint').removeClass('text-muted text-success').addClass('text-danger').text(message);
        $button.prop('disabled', false).html('批量设置');
    });
}

async function deleteFlavorPrice(productId){
    const userId=String($('#userPriceUserId').val()),url='/api/users/'+userId+'/prices/flavor/'+productId;
    $('#userPriceSaveStatus').text('');
    try{
      const response=await fetch(url,{credentials:'same-origin',cache:'no-store'}),p=await response.json();
      if(!response.ok)throw Error(p.detail||'预览失败');
      if(!confirm('仅删除商品ID '+productId+'（'+p.name+' / 仓库'+p.warehouse_id+'）的单独专属价：¥'+p.before+' → ¥'+p.after+'。恢复分类价或等级价；不修改其他口味及历史订单。是否确认？'))return;
      if(String($('#userPriceUserId').val())!==userId)throw Error('当前用户已变化，请重新操作');
      const saved=await confirmedPriceFetch(url,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Price-CSRF':p.csrf},body:JSON.stringify({token:p.token,confirm:true})});
      const result=await saved.json();if(!saved.ok)throw Error(result.detail||'删除失败，请刷新核查');
      delete userPriceDrafts[productId];
      const refresh=await fetch('/api/users/'+userId+'/prices',{credentials:'same-origin',cache:'no-store'});
      if(!refresh.ok)throw Error('已删除，刷新失败；请重新打开价格窗口');
      currentUserPriceResponse=await refresh.json();renderUserPriceRows();
      $('#userPriceSaveStatus').text('已删除此口味特殊价，当前按分类价或等级价取价。');
    }catch(e){$('#userPriceSaveStatus').text(e.message);}
}

async function reviewCategoryInheritance(parentId){
    if(Object.keys(userPriceDrafts).length||Object.keys(userCategoryPriceDrafts).length){$('#userPriceSaveStatus').text('请先保存价格草稿，再确认历史继承。');return;}
    const userId=$('#userPriceUserId').val(),url='/api/users/'+userId+'/prices/category-inheritance/'+parentId;
    try{
      const response=await fetch(url,{cache:'no-store'}),data=await response.json();
      if(!response.ok)throw Error(data.detail||'无法读取分类价格');
      $('#categoryInheritancePanel').remove();
      const panel=$('<section>').attr('id','categoryInheritancePanel').addClass('border rounded p-3 mt-3');
      panel.append($('<h6>').text('确认历史价格继承'),$('<p>').text('仅勾选需要转为跟随分类价的旧覆盖。未勾选的口味价原样保留；不会自动推断历史意图，不修改历史订单。'));
      const price=$('<input>').attr({type:'number',min:0,step:'0.01','aria-label':'明确指定分类专属价',placeholder:'明确指定分类专属价'}).addClass('form-control');
      panel.append(price);
      const checks=[];
      data.overrides.forEach(row=>{
        const product=data.products.find(p=>p.id===row.product_id);
        const check=$('<input>').attr({type:'checkbox','aria-label':'转换商品'+row.product_id});
        checks.push({id:row.product_id,input:check});
        panel.append($('<label>').addClass('d-block mt-2').append(check,document.createTextNode(' '+(product?product.name:'商品')+' / 仓库'+(product?product.warehouse_id:'?')+' / ID '+row.product_id+'，原价¥'+row.unit_price+' → 跟随分类价')));
      });
      const consent=$('<input>').attr({type:'checkbox','aria-label':'确认转换范围'});
      panel.append($('<label>').addClass('d-block mt-3').append(consent,document.createTextNode(' 我已逐项核对转换范围及保留的特殊价')));
      const error=$('<p>').attr('role','alert').addClass('text-danger');
      const save=$('<button>').attr('type','button').addClass('btn btn-primary btn-sm').text('确认保存分类继承');
      panel.append(error,save,$('<button>').attr('type','button').addClass('btn btn-light btn-sm').text('取消').on('click',()=>panel.remove()));
      $('#userPriceModal .modal-body').append(panel);
      save.on('click',async()=>{
        const raw=String(price.val()).trim(),value=Number(raw);
        if(!/^\d+(?:\.\d{1,2})?$/.test(raw)||!Number.isFinite(value)||value>1000000||!consent.prop('checked')){error.text('请输入有效分类价格（最多两位小数）并明确确认转换范围');return;}
        save.prop('disabled',true);
        try{
          const reply=await confirmedPriceFetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Price-CSRF':data.csrf},body:JSON.stringify({
            token:data.token,confirm:true,unit_price:Number(value.toFixed(2)),convert_ids:checks.filter(x=>x.input.prop('checked')).map(x=>x.id)})});
          const result=await reply.json();if(!reply.ok)throw Error(result.detail||'确认失败');
          panel.remove();showPriceModal(userId,$('#userPriceTelegramId').text(),$('#userPriceUserName').text());
        }catch(failure){error.text(failure.message);save.prop('disabled',false);}
      });
    }catch(failure){$('#userPriceSaveStatus').text(failure.message);}
}

async function unifyCanonicalUserPrice(productId){
    if(Object.keys(userPriceDrafts).length){$('#userPriceSaveStatus').text('请先保存当前价格草稿，再指定跨仓统一价，避免覆盖未保存修改。');return;}
    const rows=(currentUserPriceResponse&&currentUserPriceResponse.prices)||[];
    const source=rows.find(r=>r.product_id===productId);
    if(!source||source.logical_category_id==null){$('#userPriceSaveStatus').text('无法确认Canonical分类，请刷新目录。');return;}
    const peers=rows.filter(r=>r.logical_category_id===source.logical_category_id&&r.product_name===source.product_name);
    $('#canonicalPricePanel').remove();
    const panel=$('<section>').attr('id','canonicalPricePanel').addClass('border rounded p-3 mt-3');
    panel.append($('<h6>').text('指定跨仓统一价：'+source.product_name),
        $('<p>').text('范围：'+peers.map(r=>(r.warehouse_name||'未命名仓')+' / '+r.product_id).join('、')+'。仅修改当前客户该口味的专属价，历史订单及其他价格不变。'));
    const label=$('<label>').attr('for','canonicalSpecifiedPrice').text('最终销售单价（由管理员明确指定）');
    const input=$('<input>').attr({id:'canonicalSpecifiedPrice',type:'number',min:0,step:'0.01',placeholder:'请输入明确指定的价格'}).addClass('form-control');
    const check=$('<input>').attr({type:'checkbox',id:'canonicalPriceConfirm'});
    const consent=$('<label>').attr('for','canonicalPriceConfirm').text(' 我已核对以上商品范围，确认按输入价格统一');consent.prepend(check);
    const error=$('<p>').attr('role','alert').addClass('text-danger');
    const save=$('<button>').attr('type','button').addClass('btn btn-primary btn-sm').text('确认统一此口味价格');
    const cancel=$('<button>').attr('type','button').addClass('btn btn-outline-secondary btn-sm').text('取消统一').on('click',()=>panel.remove());
    panel.append(label,input,consent,error,$('<div>').addClass('d-flex flex-wrap gap-2').append(save,cancel));
    $('#userPriceModal .modal-body').append(panel);
    save.on('click',async()=>{
      const raw=String(input.val()).trim(),value=Number(raw);
      if(!/^\d+(?:\.\d{1,2})?$/.test(raw)||!Number.isFinite(value)||value>1000000){error.text('请输入有效非负单价（最多两位小数）');return;}
      if(!check.prop('checked')){error.text('请先核对并确认商品范围');return;}
      const price=Number(value.toFixed(2));save.prop('disabled',true);error.text('');
      try{
        await priceAwareAjax({url:'/api/users/'+$('#userPriceUserId').val()+'/prices/batch',
            method:'POST',contentType:'application/json',data:JSON.stringify({
                prices:[{product_id:productId,unit_price:price}],canonical_all_warehouses:true,
                expected_product_ids:peers.map(r=>r.product_id)})});
        panel.remove();
        showPriceModal($('#userPriceUserId').val(),$('#userPriceTelegramId').text(),$('#userPriceUserName').text());
      }catch(failure){error.text(failure.responseJSON?.detail||failure.message||'统一失败');save.prop('disabled',false);}
    });
}

function saveUserPrices() {
    if(Object.keys(userCategoryPriceDrafts).length){
        if(!categoryPricePreviewSave){$('#userPriceSaveStatus').addClass('text-danger').text('请先点击“预览分类修改”，预览通过后再保存。');return;}
        categoryPricePreviewSave.trigger('click');return;
    }
    var userId = $('#userPriceUserId').val();
    var updates = [];
    var invalid = false;
    $('.user-price-input').each(function() {
        var $input = $(this);
        var initialValue = Number($input.data('initial-value'));
        var raw = $input.val().trim();
        if (raw === '') return;
        var price = Number(raw);
        if (!/^\d+(?:\.\d{1,2})?$/.test(raw) || !Number.isFinite(price) || price > 1000000) {
            invalid = true;
            $input.addClass('is-invalid');
            return;
        }
        $input.removeClass('is-invalid');
        if (Math.abs(price - initialValue) <= 0.000001) return;
        updates.push({
            product_id: Number($input.data('product-id')),
            unit_price: Number(price.toFixed(2))
        });
    });
    if (invalid) {
        $('#userPriceSaveStatus').removeClass('text-success').addClass('text-danger').text('请修正红色输入框中的价格');
        return;
    }
    var byId=new Map(updates.map(function(row){return [row.product_id,row];}));
    ((currentUserPriceResponse && currentUserPriceResponse.prices)||[]).forEach(function(row){
        if(!Object.prototype.hasOwnProperty.call(userPriceDrafts,row.product_id))return;
        var raw=String(userPriceDrafts[row.product_id]).trim(),price=Number(raw);
        if(raw==='')return;
        if(!/^\d+(?:\.\d{1,2})?$/.test(raw)||!Number.isFinite(price)||price>1000000){invalid=true;return;}
        var initial=Number(row.custom_price==null ? row.unit_price : row.custom_price);
        if(Math.abs(price-initial)>0.000001)byId.set(row.product_id,{product_id:row.product_id,unit_price:Number(price.toFixed(2))});
    });
    if(invalid){$('#userPriceSaveStatus').text('仍有无效价格（可能在其他分类），请修正后保存');return;}
    updates=Array.from(byId.values());
    if(!updates.length){$('#userPriceSaveStatus').text('没有检测到价格修改');return;}
    var $button = $('#saveUserPricesButton');
    $button.prop('disabled', true).html('<span class="spinner-border spinner-border-sm me-1"></span>保存中...');
    $('#userPriceSaveStatus').removeClass('text-danger').addClass('text-muted').text('正在保存 ' + updates.length + ' 个商品的专属价...');
    priceAwareAjax({
        url: '/api/users/' + userId + '/prices/batch',
        method: 'POST',
        contentType: 'application/json',
        data: JSON.stringify({prices: updates}),
        processData: false
    }).done(function(resp) {
        var count = (resp && resp.updated_count) || updates.length;
        $('#userPriceSaveStatus').removeClass('text-muted text-danger').addClass('text-success').text('已保存 ' + count + ' 个商品的专属价');
        $button.prop('disabled', false).html('<i class="fas fa-save me-1"></i>保存修改');
        showPriceModal(userId, $('#userPriceTelegramId').text(), $('#userPriceUserName').text());
    }).fail(function(xhr) {
        var message = (xhr && xhr.responseJSON && (xhr.responseJSON.detail || xhr.responseJSON.message)) || '部分保存失败，请重试';
        $('#userPriceSaveStatus').removeClass('text-muted').addClass('text-danger').text(message);
        $button.prop('disabled', false).html('<i class="fas fa-save me-1"></i>保存修改');
    });
}
