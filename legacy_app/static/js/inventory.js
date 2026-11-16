let products = [];
let warehouses = [];
let stockRows = [];
let inventoryValuations = [];
let batchInboundRows = [];
let batchInboundErrors = [];

function stockUnit(row){return ['支','颗','盒','瓶','包','套','件'].includes(row.unit)?row.unit:'支';}
function stockUnitTotal(rows){
  const totals={};rows.forEach(r=>{const u=stockUnit(r);totals[u]=(totals[u]||0)+Number(r.quantity||0);});
  return ['支','颗','盒','瓶','包','套','件'].filter(u=>totals[u]).map(u=>totals[u]+u).join('、')||'0支';
}

function normalizeLowStockThreshold(value, defaultValue = 20) {
  let fallback = parseInt(defaultValue, 10);
  if (!Number.isFinite(fallback)) fallback = 20;
  if (value === null || value === undefined || String(value).trim() === '') {
    return Math.max(0, fallback);
  }
  const parsed = parseInt(value, 10);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : Math.max(0, fallback);
}

$(function(){
  loadAll();
  const today = RainbowBusinessTime.today();
  $('#inboundDay').val(today);
  loadInbounds();
});

function loadAll(){
  $.when(
    $.get('/api/products?include_inactive=true'),
    $.get('/api/warehouses'),
    $.get('/api/stock?include_inactive=true'),
    $.get('/api/stock/batches'),
    $.get('/api/stock/operations/valuation')
  ).done(function(p,w,s,b,v){
    inventoryValuations=v[0]||[];
    products = (p[0]||[]).filter(x => !x.is_parent);
    warehouses = w[0] || [];
    stockRows = normalizeStock(s[0]);
    fillSelects();
    renderStockTable();
    renderBatches(b[0]||[]);
  }).fail(function(xhr){
    alert('加载失败: ' + ((xhr.responseJSON&&xhr.responseJSON.detail)||'error'));
  });
}

function normalizeStock(data){
  // API may return list of stocks or nested
  if (!data) return [];
  if (Array.isArray(data)) {
    return data.map(x => ({
       product_id: x.product_id || (x.product && x.product.id),
       product_name: x.product_name || (x.product && x.product.name) || x.name,
       parent_name: x.parent_name || x.category || '',
       parent_id: x.parent_id || (x.product && x.product.parent_id),
       category_display_id: x.category_display_id || '',
       product_display_id: x.product_display_id || '',
       warehouse_id: x.warehouse_id || (x.warehouse && x.warehouse.id),
       warehouse_name: x.warehouse_name || (x.warehouse && x.warehouse.name),
       quantity: x.quantity || x.stock || 0,
       unit: stockUnit(x),
       cost_price: x.cost_price || (x.product && x.product.cost_price) || 0,
       low_stock_threshold: normalizeLowStockThreshold(x.low_stock_threshold),
       is_active: x.is_active !== false,
       auto_unlisted: x.auto_unlisted === true,
       value: x.value,
    }));
  }
  if (data.stocks) return normalizeStock(data.stocks);
  return [];
}

function fillSelects(){
  const warehouseSelection=$('#filterWarehouse').val(),batchSelection=$('#batchInboundWarehouse').val(),recordSelection=$('#operationWarehouseFilter').val();
  const $fw = $('#filterWarehouse').empty().append('<option value="">全部仓库</option>');
  const $iw = $('#inWarehouse').empty();
  const $biw = $('#batchInboundWarehouse').empty();
  warehouses.forEach(w => {
    $fw.append(`<option value="${w.id}">${w.name}</option>`);
    if (w.is_active === false) return;
    $iw.append(`<option value="${w.id}">${w.name}</option>`);
    $biw.append(`<option value="${w.id}">${w.name}</option>`);
  });
  $('#filterWarehouse').val(warehouseSelection);if(batchSelection)$biw.val(batchSelection);
  const recordWarehouse=$('#operationWarehouseFilter').empty().append($('<option>').val('').text('全部仓库'));
  warehouses.forEach(w=>recordWarehouse.append($('<option>').val(w.id).text(w.name)));recordWarehouse.val(recordSelection);
  const $ip = $('#inProduct').empty();
  products.forEach(p => {
    const category = p.parent_name || '未分类';
    const productId = p.product_display_id || ('P' + String(p.product_serial || p.id).padStart(3, '0'));
    $ip.append(`<option value="${p.id}" data-cost="${p.cost_price||0}">${category} / ${productId} ${p.name}</option>`);
  });
  $('#inProduct').off('change').on('change', function(){
    const cost = $(this).find(':selected').data('cost');
    $('#inCost').val('').attr('placeholder','填写本批进货价；留空使用商品当前成本');
  }).trigger('change');
}

function renderStockTable(){
  const wid = $('#filterWarehouse').val();
  const kw = ($('#filterKeyword').val()||'').trim().toLowerCase();
  let rows = stockRows.slice();
  if (wid) rows = rows.filter(r => String(r.warehouse_id) === String(wid));
  if (kw) rows = rows.filter(r => String(r.product_name||'').toLowerCase().includes(kw));
  const $container = $('#inventory-category-list').empty();
  if (!rows.length){
    $container.append('<div class="text-center text-muted py-4">暂无库存</div>');
    $('#stockMeta').text('0 条');
    return;
  }
  let totalValue = 0;
  const groups = {};
  rows.forEach(r => {
    const valuation=inventoryValuations.find(v=>v.product_id===r.product_id&&v.warehouse_id===r.warehouse_id);
    const val = valuation ? Number(valuation.verified_value) : 0;
    r.reference_value=valuation ? valuation.reference_value : null;
    r.reference_unit_price=valuation ? valuation.reference_unit_price : null;
    r.reference_price_state=valuation ? valuation.reference_price_state : 'UNSET';
    r.reference_incomplete=!valuation||valuation.reference_incomplete||valuation.reference_value==null;
    r.cost_incomplete=!valuation||valuation.cost_incomplete;
    totalValue += val;
    const key = r.warehouse_id+'-'+String(r.parent_id || r.parent_name || '0');
    if (!groups[key]) groups[key] = [];
    groups[key].push(Object.assign({}, r, {_value: val}));
  });
  const warehouseTotals={};
  rows.forEach(r=>{const w=warehouseTotals[r.warehouse_id]||(warehouseTotals[r.warehouse_id]={name:r.warehouse_name,quantity:0,reference:0,verified:0,incomplete:false});w.quantity+=Number(r.quantity);w.reference+=Number(r.reference_value||0);w.verified+=inventoryValuations.find(v=>v.product_id===r.product_id&&v.warehouse_id===r.warehouse_id)?.verified_value||0;w.incomplete=w.incomplete||r.reference_incomplete;});
  Object.entries(warehouseTotals).forEach(([id,w])=>$container.append($('<div>').addClass('small text-muted mb-2').text(w.name+' / 总计'+stockUnitTotal(rows.filter(r=>String(r.warehouse_id)===id))+' / '+(w.incomplete?'参考金额不完整，已填写部分 ¥':'库存金额 ¥')+w.reference.toFixed(2)+referenceMissingCounts(rows.filter(r=>String(r.warehouse_id)===id)))));
  Object.keys(groups).sort().forEach(function(categoryKey) {
    const items = groups[categoryKey];
    const categoryName = items[0].parent_name || '未分类';
    const categoryId = items[0].category_display_id || '';
    const categoryTotal = items.reduce((sum, item) => sum + Number(item.quantity || 0), 0);
    const collapseId = 'inventory-category-' + categoryKey.replace(/[^0-9a-z_-]/gi, '');
    const rowsHtml = items.map(function(r) {
      const qty = Number(r.quantity || 0);
      const threshold = normalizeLowStockThreshold(r.low_stock_threshold);
      const stockClass = qty <= 0 ? 'danger' : (threshold > 0 && qty <= threshold ? 'warning' : 'success');
      const status = r.auto_unlisted
        ? '<span class="badge bg-warning text-dark">TG停售</span>'
        : (r.is_active ? '<span class="badge bg-success">正常</span>' : '<span class="badge bg-secondary">手动下架</span>');
      return `
        <tr>
          <td><strong>${escapeHtml(r.product_name || '')}</strong><div class="small text-muted">${escapeHtml(r.warehouse_name || '')}</div></td>
          <td>
            <span>${qty}${stockUnit(r)}</span>
          </td>
          <td>${r.reference_price_state==='UNKNOWN_LEGACY'?'成本待确认':(r.reference_price_state==='UNSET'?'未填写':'¥'+Number(r.reference_unit_price).toFixed(2)+' · 已填写')}</td>
          <td>${r.reference_incomplete?(r.reference_price_state==='UNKNOWN_LEGACY'?'参考金额不完整 / 成本待确认':'参考金额不完整 / 缺少成本'):'参考 ¥'+Number(r.reference_value).toFixed(2)}</td>
        </tr>`;
    }).join('');
    $container.append(`
      <div class="border rounded overflow-hidden mb-2">
        <button type="button" class="btn btn-light w-100 text-start d-flex justify-content-between align-items-center px-3 py-3" onclick="$('#${collapseId}').collapse('toggle')">
          <span><span class="me-2">▶</span><strong>${escapeHtml(items[0].warehouse_name)} / ${escapeHtml(categoryName)}</strong></span>
          <span class="d-flex align-items-center gap-2">
            <span><span class="badge bg-light text-dark me-2">${items.length}个口味</span><span class="sk-meta">总计${stockUnitTotal(items)} / ${items.some(r=>r.reference_incomplete)?'参考金额不完整，已填写部分':'库存金额'} ¥${items.reduce((n,r)=>n+Number(r.reference_value||0),0).toFixed(2)}${referenceMissingCounts(items)}</span></span>
          </span>
        </button>
        <div id="${collapseId}" class="collapse">
          <div class="table-responsive">
            <table class="table table-hover table-sm mb-0 align-middle">
              <thead><tr><th>商品 / 口味</th><th>库存</th><th>成本</th><th>库存金额</th></tr></thead>
              <tbody>${rowsHtml}</tbody>
            </table>
          </div>
        </div>
      </div>`);
  });
  const referenceSum=rows.reduce((n,r)=>n+Number(r.reference_value||0),0);
  const referenceLabel=rows.some(r=>r.reference_incomplete)?'参考金额不完整，已填写部分 ¥':'参考库存金额 ¥';
  $('#stockMeta').text(rows.length + '种商品 / 总库存'+stockUnitTotal(rows)+' / '+referenceLabel+referenceSum.toFixed(2)+referenceMissingCounts(rows));
}

function referenceMissingCounts(rows){
  const missing=rows.filter(r=>Number(r.quantity)>0&&r.reference_price_state==='UNSET').length;
  const unknown=rows.filter(r=>Number(r.quantity)>0&&r.reference_price_state==='UNKNOWN_LEGACY').length;
  return missing||unknown?' / 缺价 '+missing+' 商品 / 待确认 '+unknown+' 商品':'';
}

function getChangedInventoryStockInputs(groupKey) {
  return $('input.stock-inline-input[data-stock-group="' + groupKey + '"]').filter(function(){
    const input = $(this);
    const quantity = Math.max(0, parseInt(input.val(), 10) || 0);
    const old = Number(input.data('raw-value') || 0);
    return quantity !== old;
  });
}

function updateInventoryStockSaveButton(groupKey) {
  const count = getChangedInventoryStockInputs(groupKey).length;
  const btn = $('.stock-save-btn[data-stock-save-group="' + groupKey + '"]');
  btn.toggleClass('btn-primary', count > 0).toggleClass('btn-outline-primary', count === 0);
  btn.text(count > 0 ? ('确认库存修改 (' + count + ')') : '确认库存修改');
}

$(document).on('input change', 'input.stock-inline-input', function(){
  const input = $(this);
  const quantity = Math.max(0, parseInt(input.val(), 10) || 0);
  const old = Number(input.data('raw-value') || 0);
  input.toggleClass('border-warning bg-warning-subtle', quantity !== old);
  updateInventoryStockSaveButton(input.data('stock-group'));
});

function submitInventoryStockGroup(groupKey) {
  const changed = getChangedInventoryStockInputs(groupKey);
  if (!changed.length) {
    alert('这个品类没有库存改动。');
    return;
  }
  if (!confirm('确认保存 ' + changed.length + ' 个库存修改吗？')) return;
  const btn = $('.stock-save-btn[data-stock-save-group="' + groupKey + '"]');
  btn.prop('disabled', true).text('保存中...');
  const requests = changed.map(function(){
    const input = $(this);
    return $.post('/api/stock/update', {
      product_id: input.data('product-id'),
      warehouse_id: input.data('warehouse-id'),
      quantity: Math.max(0, parseInt(input.val(), 10) || 0),
      mode: 'set',
      note: '后台到货入库页批量确认库存'
    });
  }).get();
  $.when.apply($, requests).done(function(){
    alert('库存修改已保存，共 ' + changed.length + ' 个。');
    loadAll();
  }).fail(function(xhr){
    alert((xhr.responseJSON && xhr.responseJSON.detail) || '库存修改失败');
    loadAll();
  });
}

function renderBatches(rows){
  const $tb = $('#batch-tbody').empty();
  if (!rows.length){ $tb.append('<tr><td colspan="4" class="text-center text-muted">暂无剩余批次</td></tr>'); return; }
  rows.slice(0,100).forEach(b => {
    $tb.append(`<tr>
      <td class="sk-meta">${escapeHtml(b.batch_no||'')}</td>
      <td>${escapeHtml(b.product_name||'')}<div class="sk-meta">${escapeHtml(b.warehouse_name||'')}</div></td>
      <td>${b.remain_qty||0}</td>
      <td>${b.cost_verified ? '¥'+Number(b.unit_cost||0).toFixed(2) : '历史进货价缺失'}<div class="sk-meta">${b.cost_verified ? '本批进货价' : '原记录保留用于审计'}</div></td>
    </tr>`);
  });
}

function showInboundModal(){
  fillSelects();
  $('#inQty').val(1);
  $('#inNote').val('');
  $('#inboundModal').modal('show');
}
function showBatchInboundModal(){
  fillSelects();
  if (!$('#batchInboundWarehouse').val() && warehouses[0]) {
    $('#batchInboundWarehouse').val(String(warehouses[0].id));
  }
  $('#batchInboundText').val('悦刻ACE，冰爽铁观音，20，浓郁铁观音20');
  batchInboundRows = [];
  batchInboundErrors = [];
  $('#batchInboundSummary').text('尚未预览');
  $('#batchInboundPreviewBody').html('<tr><td colspan="8" class="text-center text-muted">先预览再提交</td></tr>');
  $('#batchInboundModal').modal('show');
}
function quickInbound(pid, wid, cost){
  fillSelects();
  $('#inProduct').val(String(pid));
  $('#inWarehouse').val(String(wid));
  $('#inCost').val('').attr('placeholder','参考价 '+cost+'，请填写本批进货价');
  $('#inQty').val(1);
  $('#inboundModal').modal('show');
}

function adjustInventoryStock(pid, wid, operation) {
  const raw = window.prompt(operation === 'subtract' ? '请输入减少数量' : '请输入增加数量', '1');
  const qty = Number(raw);
  if (!Number.isInteger(qty) || qty <= 0) { if (raw !== null) alert('数量必须是正整数'); return; }
  $.post('/api/stock/update', { product_id: pid, warehouse_id: wid, quantity: qty, mode: operation, note: '后台库存调整' })
    .done(function(resp){ alert((resp && resp.message) || '库存已更新'); loadAll(); })
    .fail(function(xhr){ alert((xhr.responseJSON && xhr.responseJSON.detail) || '库存更新失败'); });
}

function showBatchAdjustModal() {
  fillSelects();
  const $select = $('#adjustWarehouse').empty();
  warehouses.forEach(function(w){ if (w.is_active !== false) $select.append(`<option value="${w.id}">${escapeHtml(w.name || '')}</option>`); });
  $('#batchAdjustText').val('');
  $('#adjustOperation').val('add');
  $('#batchAdjustModal').modal('show');
}

function submitBatchAdjust() {
  const operation = $('#adjustOperation').val() || 'add';
  const warehouseId = $('#adjustWarehouse').val();
  const lines = ($('#batchAdjustText').val() || '').split(/\r?\n/).map(function(line){ return line.trim(); }).filter(Boolean);
  if (!warehouseId || !lines.length) { alert('请填写仓库和批量数据'); return; }
  const requests = [];
  const errors = [];
  lines.forEach(function(line, index){
    const parts = line.split(/[\t,，;；]/).map(function(v){ return v.trim(); });
    if (parts.length < 2) { errors.push('第' + (index + 1) + '行格式错误'); return; }
    const ref = parts[0]; const qty = Number(parts[1]);
    if (!Number.isInteger(qty) || qty <= 0) { errors.push('第' + (index + 1) + '行数量无效'); return; }
    const product = products.find(function(p){
      const display = String(p.product_display_id || ('P' + String(p.id).padStart(3, '0'))).toLowerCase();
      return String(p.id) === ref || display === ref.toLowerCase() || String(p.name || '').toLowerCase() === ref.toLowerCase();
    });
    if (!product) { errors.push('第' + (index + 1) + '行商品不存在: ' + ref); return; }
    requests.push($.post('/api/stock/update', { product_id: product.id, warehouse_id: warehouseId, quantity: qty, mode: operation, note: '后台批量库存调整' }));
  });
  if (errors.length) { alert(errors.join('\n')); if (!requests.length) return; }
  $.when.apply($, requests).done(function(){ alert('批量库存调整已完成 ' + requests.length + ' 项'); $('#batchAdjustModal').modal('hide'); loadAll(); })
    .fail(function(xhr){ alert((xhr.responseJSON && xhr.responseJSON.detail) || '批量库存调整失败'); loadAll(); });
}

function submitInbound(){
  const product_id = $('#inProduct').val();
  const warehouse_id = $('#inWarehouse').val();
  const quantity = parseInt($('#inQty').val(),10)||0;
  const unit_cost = $('#inCost').val();
  const note = $('#inNote').val();
  if (!product_id || !warehouse_id || quantity<=0){ alert('请完整填写入库信息'); return; }
  $.post('/api/stock/inbound', {
    product_id, warehouse_id, quantity, unit_cost, note
  }).done(function(resp){
    alert(`入库成功：${resp.product_name} +${resp.quantity}，批次价 ¥${Number(resp.unit_cost||0).toFixed(2)}`);
    $('#inboundModal').modal('hide');
    loadAll();
    loadInbounds();
  }).fail(function(xhr){
    alert((xhr.responseJSON&&xhr.responseJSON.detail)||'入库失败');
  });
}
function splitBatchInboundLine(line){
  var parts;
  if (line.indexOf('\t') >= 0) {
    parts = line.split('\t').map(function(part){ return part.trim(); });
  } else {
    parts = line.split(/[,，]/).map(function(part){ return part.trim(); });
  }
  while (parts.length && parts[parts.length - 1] === '') parts.pop();
  return parts;
}
function looksLikeBatchHeader(parts){
  var joined = (parts || []).join('');
  return /品类|分类|口味|仓库|数量|单价/.test(joined);
}
function isNumberToken(value) {
  var text = String(value == null ? '' : value).trim();
  return text !== '' && Number.isFinite(Number(text));
}
function splitTrailingQuantityToken(value) {
  var text = String(value == null ? '' : value).trim();
  var match = text.match(/^(.+?)(?:\s*[xX*×]\s*)?(-?\d+(?:\.\d+)?)$/);
  if (!match) return null;
  var name = String(match[1] || '').trim().replace(/[，,、:：;；-]+$/g, '').trim();
  var quantity = String(match[2] || '').trim();
  if (!name || !isNumberToken(quantity)) return null;
  return { name: name, quantity: quantity };
}
function parseNameQuantityPairs(parts, startIndex) {
  var pairs = [];
  for (var i = startIndex; i < parts.length;) {
    var name = String(parts[i] || '').trim();
    if (!name) return null;
    if (i + 1 < parts.length && isNumberToken(parts[i + 1])) {
      pairs.push({ name: name, quantity: parts[i + 1] });
      i += 2;
      continue;
    }
    var embedded = splitTrailingQuantityToken(name);
    if (embedded) {
      pairs.push(embedded);
      i += 1;
      continue;
    }
    return null;
  }
  return pairs.length ? pairs : null;
}
function getDefaultBatchInboundWarehouse() {
  var id = $('#batchInboundWarehouse').val();
  var name = $('#batchInboundWarehouse option:selected').text();
  if (!id || !name) return null;
  return { id: parseInt(id, 10), name: name };
}
function pushBatchInboundRow(rows, errors, index, categoryName, productName, warehouseName, warehouseId, quantityRaw, unitCostRaw, note) {
  var quantity = parseInt(quantityRaw, 10);
  var unitCostText = unitCostRaw == null ? '' : String(unitCostRaw).trim();
  var unitCost = unitCostText === '' ? null : parseFloat(unitCostText);
  var rowErrors = [];
  if (!productName) rowErrors.push('口味名称不能为空');
  if (!warehouseName && !warehouseId) rowErrors.push('仓库不能为空');
  if (!Number.isFinite(quantity) || quantity < 0) rowErrors.push('数量不能小于0');
  if (unitCostText !== '' && !Number.isFinite(unitCost)) rowErrors.push('单价格式不正确');
  rowErrors.forEach(function(msg){ errors.push('第' + (index + 1) + '行' + msg); });
  var skipped = Number.isFinite(quantity) && quantity === 0;
  rows.push({
    line_no: index + 1,
    category_name: categoryName || '',
    product_name: productName || '',
    warehouse_name: warehouseName || '',
    warehouse_id: warehouseId || null,
    quantity: Number.isFinite(quantity) ? quantity : 0,
    unit_cost: unitCost,
    note: String(note || '').trim(),
    skipped: skipped,
    errors: rowErrors,
  });
}
function parseBatchInboundText(text){
  var lines = String(text || '').replace(/\r/g, '').split('\n');
  var rows = [];
  var errors = [];
  var defaultWarehouse = getDefaultBatchInboundWarehouse();
  lines.forEach(function(line, index){
    var raw = line.trim();
    if (!raw || raw.indexOf('#') === 0) return;
    var parts = splitBatchInboundLine(raw);
    if (rows.length === 0 && looksLikeBatchHeader(parts)) return;
    if (parts.length < 3) {
      errors.push('第' + (index + 1) + '行列数不足');
      return;
    }
    var categoryName = parts[0] || '';

    if (defaultWarehouse) {
      var pairs = parseNameQuantityPairs(parts, 1);
      if (pairs) {
        pairs.forEach(function(pair) {
          pushBatchInboundRow(rows, errors, index, categoryName, pair.name, defaultWarehouse.name, defaultWarehouse.id, pair.quantity, '', '');
        });
        return;
      }
      if (isNumberToken(parts[2])) {
        pushBatchInboundRow(rows, errors, index, categoryName, parts[1] || '', defaultWarehouse.name, defaultWarehouse.id, parts[2], parts[3] || '', parts.slice(4).join(','));
        return;
      }
    }

    if (!defaultWarehouse && isNumberToken(parts[2])) {
      errors.push('第' + (index + 1) + '行缺少仓库：请选择默认入库仓库，或在第三列填写仓库名');
      return;
    }

    if (parts.length < 4) {
      errors.push('第' + (index + 1) + '行列数不足');
      return;
    }
    pushBatchInboundRow(rows, errors, index, categoryName, parts[1] || '', parts[2] || '', null, parts[3], parts[4] || '', parts.slice(5).join(','));
  });
  return {rows: rows, errors: errors};
}
function renderBatchInboundPreview(){
  var $body = $('#batchInboundPreviewBody').empty();
  if (!batchInboundRows.length) {
    $body.append('<tr><td colspan="8" class="text-center text-muted">先预览再提交</td></tr>');
  } else {
    batchInboundRows.forEach(function(row){
      var rowErrors = row.errors || [];
      var statusBadge = rowErrors.length
        ? '<span class="badge bg-danger">' + escapeHtml(rowErrors.join('；')) + '</span>'
        : (row.skipped
          ? '<span class="badge bg-secondary">数量为0，跳过</span>'
          : '<span class="badge bg-success">可导入</span>');
      $body.append(
        '<tr>' +
          '<td class="sk-meta">' + escapeHtml(row.line_no) + '</td>' +
          '<td>' + escapeHtml(row.category_name || '默认分类') + '</td>' +
          '<td>' + escapeHtml(row.product_name || '') + '</td>' +
          '<td>' + escapeHtml(row.warehouse_name || '') + '</td>' +
          '<td>' + escapeHtml(row.quantity) + '</td>' +
          '<td>' + (row.unit_cost === null || row.unit_cost === undefined || row.unit_cost === '' ? '-' : '¥' + Number(row.unit_cost).toFixed(2)) + '</td>' +
          '<td>' + escapeHtml(row.note || '') + '</td>' +
          '<td>' + statusBadge + '</td>' +
        '</tr>'
      );
    });
  }
  var summary = '预览 ' + batchInboundRows.length + ' 行';
  var skippedCount = batchInboundRows.filter(function(row){ return row.skipped; }).length;
  if (skippedCount) summary += '，跳过 ' + skippedCount + ' 行数量为0';
  if (batchInboundErrors.length) summary += '，发现 ' + batchInboundErrors.length + ' 个问题';
  $('#batchInboundSummary').text(summary + (batchInboundErrors.length ? '：' + batchInboundErrors.slice(0, 3).join('；') : ''));
}
function previewBatchInbound(){
  var parsed = parseBatchInboundText($('#batchInboundText').val());
  batchInboundRows = parsed.rows;
  batchInboundErrors = parsed.errors;
  renderBatchInboundPreview();
  return parsed;
}
function submitBatchInbound(){
  var parsed = previewBatchInbound();
  if (!parsed.rows.length) {
    alert('请先粘贴批量入库数据');
    return;
  }
  if (parsed.errors.length) {
    alert('存在校验错误，请先修正后再提交。');
    return;
  }
  var rowsToSubmit = parsed.rows.filter(function(row){ return !row.skipped && Number(row.quantity || 0) > 0; });
  if (!rowsToSubmit.length) {
    alert('没有需要入库的数量：数量为0的行会自动跳过。');
    return;
  }
  if (!confirm('确认批量入库 ' + rowsToSubmit.length + ' 行吗？')) return;
  $.ajax({
    url: '/api/stock/batch_inbound',
    method: 'POST',
    contentType: 'application/json',
    data: JSON.stringify({items: rowsToSubmit}),
    success: function(resp){
      alert('批量入库成功，共 ' + (resp.created_count || rowsToSubmit.length) + ' 行');
      $('#batchInboundModal').modal('hide');
      loadAll();
      loadInbounds();
    },
    error: function(xhr){
      alert((xhr.responseJSON && xhr.responseJSON.detail) || '批量入库失败');
    }
  });
}
function deleteStock(wid, pid, name){
  if (!confirm('确定删除库存记录「'+name+'」吗？会清零该仓库此商品库存。')) return;
  $.post('/api/stock/delete', {warehouse_id: wid, product_id: pid}).done(function(){
    loadAll();
  }).fail(function(xhr){ alert((xhr.responseJSON&&xhr.responseJSON.detail)||'删除失败'); });
}
function loadInbounds(){
  const day = $('#inboundDay').val();
  const url = day ? ('/api/stock/inbounds?day='+encodeURIComponent(day)) : '/api/stock/inbounds';
  $.get(url, function(rows){
    const $tb = $('#inbound-tbody').empty();
    if (!rows || !rows.length){ $tb.append('<tr><td colspan="5" class="text-center text-muted">无入库记录</td></tr>'); return; }
    rows.forEach(r => {
      const t = RainbowBusinessTime.format(r.created_at);
      $tb.append(`<tr>
        <td class="sk-meta">${t}</td>
        <td><strong>${escapeHtml(r.parent_name||'未分类')}</strong><div>${escapeHtml(r.product_name||'')}</div><div class="sk-meta">${escapeHtml(r.warehouse_name||'')} ${escapeHtml(r.batch_no||'')}</div></td>
        <td>+${r.quantity||0}</td>
        <td>¥${Number(r.unit_cost||0).toFixed(2)}</td>
        <td>¥${Number(r.amount||0).toFixed(2)}</td>
      </tr>`);
    });
  });
}
function escapeHtml(s){ return String(s||'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escapeAttr(s){ return escapeHtml(s).replace(/"/g,'&quot;'); }
