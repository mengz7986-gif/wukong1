let addressRows = {};
const modeLabels = {province:'省份', city:'市区', detail:'详细地址', contains:'旧包含匹配', exact:'完全匹配'};
function loadList(){
  const kw = ($('#keyword').val()||'').trim();
  const url = '/api/blocked_addresses' + (kw ? ('?keyword=' + encodeURIComponent(kw)) : '');
  $.get(url, function(rows){
    const tb = $('#list-tbody').empty();
    if(!rows || !rows.length){
      tb.append('<tr><td colspan="6" class="text-center text-muted">暂无异常地址</td></tr>');
      return;
    }
    rows.forEach(function(r){
      addressRows[r.id] = r;
      const status = r.is_active ? '<span class="badge bg-danger">拦截中</span>' : '<span class="badge bg-secondary">已停用</span>';
      tb.append(`
        <tr>
          <td class="sk-meta">${r.id}</td>
          <td><div>${escapeHtml(r.address_text||'')}</div><div class="sk-meta">${escapeHtml(r.normalized||'')}</div></td>
          <td>${escapeHtml(modeLabels[r.match_mode]||r.match_mode)}</td>
          <td>${escapeHtml(r.reason||'')}</td>
          <td>${status}</td>
          <td>
            <button class="btn btn-sm btn-outline-primary" onclick="editItem(${r.id})">编辑</button>
            <button class="btn btn-sm btn-outline-secondary" onclick="toggleItem(${r.id})">${r.is_active?'停用':'启用'}</button>
            <button class="btn btn-sm btn-outline-danger" onclick="delItem(${r.id})">删除</button>
          </td>
        </tr>`);
    });
  }).fail(function(xhr){
    $('#list-tbody').html('<tr><td colspan="6" class="text-danger text-center">加载失败</td></tr>');
  });
}
function addOne(){
  const address_text = ($('#oneAddress').val()||'').trim();
  if(!address_text){ alert('请输入地址'); return; }
  $.post('/api/blocked_addresses', {
    address_text,
    reason: $('#reason').val(),
    match_mode: $('#matchMode').val()
  }).done(function(){
    $('#oneAddress').val('');
    loadList();
    alert('已添加');
  }).fail(function(xhr){
    alert((xhr.responseJSON&&xhr.responseJSON.detail)||'添加失败');
  });
}
function bulkAdd(){
  const content = ($('#bulkContent').val()||'').trim();
  if(!content){ alert('请粘贴地址列表'); return; }
  $.post('/api/blocked_addresses/bulk', {
    content,
    reason: $('#reason').val() || '诈骗地址',
    match_mode: $('#matchMode').val()
  }).done(function(resp){
    alert(`导入完成：成功 ${resp.added_count||0}，跳过 ${resp.skipped_count||0}`);
    $('#bulkContent').val('');
    loadList();
  }).fail(function(xhr){
    alert((xhr.responseJSON&&xhr.responseJSON.detail)||'导入失败');
  });
}
function delItem(id){
  if(!confirm('确定删除该异常地址规则吗？')) return;
  $.post('/api/blocked_addresses/' + id + '/delete', {hard: true}).done(function(){
    loadList();
  }).fail(function(xhr){
    alert((xhr.responseJSON&&xhr.responseJSON.detail)||'删除失败');
  });
}
function saveItem(r){
  return $.post('/api/blocked_addresses/'+r.id+'/update', r).done(loadList).fail(function(xhr){
    alert((xhr.responseJSON&&xhr.responseJSON.detail)||'更新失败');
  });
}
function toggleItem(id){
  const r = addressRows[id];
  if(!r || !confirm(r.is_active?'确定停用此规则？':'确定启用此规则？')) return;
  saveItem({...r,is_active:!r.is_active});
}
function editItem(id){
  const r = addressRows[id];
  if(!r) return;
  $('#editAddressForm').attr('data-id',id);
  $('#editAddressText').val(r.address_text);
  $('#editAddressReason').val(r.reason||'');
  $('#editAddressMode').val(r.match_mode);
  $('#editAddressActive').prop('checked',r.is_active);
  bootstrap.Modal.getOrCreateInstance(document.getElementById('editAddressModal')).show();
}
function saveEditedAddress(){
  const r=addressRows[Number($('#editAddressForm').attr('data-id'))];
  if(!r) return;
  saveItem({...r,address_text:$('#editAddressText').val(),reason:$('#editAddressReason').val(),
    match_mode:$('#editAddressMode').val(),is_active:$('#editAddressActive').prop('checked')})
    .done(()=>bootstrap.Modal.getOrCreateInstance(document.getElementById('editAddressModal')).hide());
}
function checkAddr(){
  const address_text = ($('#checkAddress').val()||'').trim();
  if(!address_text){ alert('请输入测试地址'); return; }
  $.post('/api/blocked_addresses/check', {address_text}).done(function(resp){
    if(resp.blocked){
      const m = resp.matched || {};
      $('#checkResult').html('<span class="text-danger">会拦截</span>：命中 ' + escapeHtml(m.address_text||'') + '（' + escapeHtml(m.reason||'') + '）');
    }else{
      $('#checkResult').html('<span class="text-success">不会拦截</span>');
    }
  }).fail(function(){
    $('#checkResult').text('检测失败');
  });
}
function escapeHtml(s){
  return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
$(loadList);
