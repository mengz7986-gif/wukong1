// Upgrades the existing batch-inbound modal; outbound shares the same controls.
let stockCsrf='', stockCanManage=false, stockCategories=[], stockPreviewToken=0;
function stockOperationUnit(row){return ['支','颗','盒','瓶','包','套','件'].includes(row.unit)?row.unit:'（单位未确认）';}
function stockQuantitySummary(rows){const totals=new Map();rows.forEach(row=>{const unit=stockOperationUnit(row);totals.set(unit,(totals.get(unit)||0)+Number(row.quantity));});return Array.from(totals,([unit,quantity])=>quantity+unit).join('、');}
function stockUnitProvenance(row){return row.unit_source==='current_catalog_not_historical_proof'?'（按当前单位展示，历史单位未留存）':row.unit_source==='historical_unknown'?'（历史单位未知）':'';}
async function stockRequest(path,body){
  const response=await fetch('/api/stock/operations'+path,body?{method:'POST',headers:{'Content-Type':'application/json','X-Stock-CSRF':stockCsrf},body:JSON.stringify(body)}:{cache:'no-store'});
  const data=await response.json(); if(!response.ok)throw Error(data.detail||'操作失败'); return data;
}
function stockPayload(){const data={warehouse_id:$('#batchInboundWarehouse').val(),category_id:$('#stockOperationCategory').val(),text:$('#batchInboundText').val(),operation_type:$('#stockOperationType').val(),note:$('#stockOperationNote').val()||''};if(data.operation_type==='inbound'&&$('#stockOperationCost').val().trim()!=='')data.actual_unit_cost=$('#stockOperationCost').val();return data;}
function stockCategoriesForWarehouse(){const select=$('#stockOperationCategory').empty().append($('<option>').val('').text('请选择分类'));const warehouse=$('#batchInboundWarehouse').val();if(warehouse)LogicalCategories.options(select,stockCategories,warehouse,true);}
async function showBatchInboundModal(kind='inbound'){
  try{
    const access=await stockRequest('/access');stockCsrf=access.csrf;stockCanManage=access.can_manage;
    const response=await fetch('/api/products/parents');if(!response.ok)throw Error('读取分类失败');stockCategories=await response.json();
    $('#stockOperationType').val(kind);$('#stockOperationTitle').text(kind==='outbound'?'批量出库':'批量入库');
    $('#batchInboundText,#stockOperationNote,#stockOperationCost').val('');$('#stockOperationCostGroup').toggle(kind==='inbound');$('#batchInboundPreviewBody').empty();
    $('#stockExecuteConfirm').prop('checked',false);
    $('#batchInboundSummary').text(stockCanManage?'先预览再提交':'当前安全门禁锁定库存写操作，可以预览');
    $('#stockOperationSubmit').prop('disabled',!stockCanManage);stockCategoriesForWarehouse();$('#batchInboundModal').modal('show');
  }catch(e){$('#stockOperationsNotice').text(e.message);}
}
async function previewBatchInbound(){
  const token=++stockPreviewToken;
  try{
    const rows=await stockRequest('/preview',stockPayload());if(token!==stockPreviewToken)return;
    const body=$('#batchInboundPreviewBody').empty();
    rows.forEach(r=>{const tr=$('<tr>'),unit=stockOperationUnit(r);[r.product_name,r.current_quantity+unit,r.quantity+unit,r.resulting_quantity+unit,r.unit_cost==null?'FIFO计算（未知成本不估价）':'¥'+Number(r.unit_cost).toFixed(2)+'/'+unit+' · 来源：'+(r.cost_origin||'本批填写'),r.match].forEach(v=>tr.append($('<td>').text(v)));body.append(tr);});
    $('#batchInboundSummary').text('仓库：'+$('#batchInboundWarehouse option:selected').text()+' / 分类：'+$('#stockOperationCategory option:selected').text()+' / '+($('#stockOperationType').val()==='inbound'?'入库':'出库')+' / '+rows.length+' 种商品 / '+stockQuantitySummary(rows)+' / 成本：'+($('#stockOperationType').val()==='inbound'?($('#stockOperationCost').val()||'各商品已确认成本'):'既有FIFO；未知成本不估价')+'；提交前服务器会重新校验');return true;
  }catch(e){if(token===stockPreviewToken){$('#batchInboundPreviewBody').empty();$('#batchInboundSummary').text(e.message);}return false;}
}
async function submitBatchInbound(){
  if(!stockCanManage||$('#stockOperationSubmit').prop('disabled'))return;
  $('#stockOperationSubmit').prop('disabled',true);
  try{
    if(!await previewBatchInbound())return;
    if(!$('#stockExecuteConfirm').prop('checked')){$('#batchInboundSummary').text('请核对预览并勾选整批操作确认');return;}
    const result=await stockRequest('',stockPayload());$('#batchInboundModal').modal('hide');
    $('#stockOperationsNotice').text('已完成批次 '+result.id);loadAll();await loadStockOperations();await loadHistoricalCosts();
  }catch(e){$('#batchInboundSummary').text(e.message);}finally{$('#stockOperationSubmit').prop('disabled',!stockCanManage);}
}
async function loadStockOperations(){
  try{
    const query=new URLSearchParams();[['warehouse_id','operationWarehouseFilter'],['logical_category_id','operationCategoryFilter'],['operation_type','operationKindFilter'],['start_date','operationStartFilter'],['end_date','operationEndFilter'],['q','operationSearchFilter']].forEach(([key,id])=>{const value=$('#'+id).val();if(value)query.set(key,value);});
    const range=RainbowTimeRange.values('operationStartFilter','operationEndFilter');
    if(range.start)query.set('start_date',range.start);if(range.end)query.set('end_date',range.end);
    const rows=await stockRequest('?'+query);const box=$('#stockOperationsList').empty();
    const table=$('<table>').addClass('table sk-table table-sm').append($('<thead>').append($('<tr>').append(...['批次号','时间','类型','仓库','分类','商品数','数量（按单位）','货值','管理员','状态 / 操作'].map(label=>$('<th>').text(label)))));
    const body=$('<tbody>');table.append(body);box.append($('<div>').addClass('table-responsive').append(table));
    rows.forEach(row=>{
      const card=$('<tr>');[row.id,RainbowBusinessTime.format(row.created_at),row.operation_type==='inbound'?'入库':'出库',row.warehouse_name,row.category_name,row.lines.length,stockQuantitySummary(row.lines),row.total_cost==null?'历史进货价缺失':'¥'+row.total_cost,row.actor].forEach(value=>card.append($('<td>').text(value)));
      const actions=$('<td>').append($('<span>').text(row.status==='completed'?'已完成':'已取消'));card.append(actions);
      const details=$('<details>').append($('<summary>').text('查看商品明细'));
      row.lines.forEach(line=>{const unit=stockOperationUnit(line);details.append($('<div>').text(line.product_name+'：'+line.before_quantity+unit+' → '+line.after_quantity+unit+'，数量 '+line.quantity+unit+'，实际成本 '+(line.unit_cost==null?'待确认':'¥'+line.unit_cost+'/'+unit)+stockUnitProvenance(line)));});details.append($('<p>').text('备注：'+row.note));actions.append(details);
      if(row.status==='completed'){
        const button=$('<button>').addClass('btn btn-outline-danger btn-sm').text(row.operation_type==='inbound'?'取消入库':'取消出库').prop('disabled',!stockCanManage);
        button.on('click',()=>openStockCancellation(row));actions.append(button);
      }else details.append($('<p>').text('取消人 '+row.cancelled_by+' · '+row.cancelled_at+' · '+row.cancel_reason));
      body.append(card);
    });
  }catch(e){$('#stockOperationsNotice').text(e.message);}
}
$(function(){
  $('#stockRecordsCard').insertAfter('#currentInventoryArea');
  fetch('/api/products/parents').then(r=>{if(!r.ok)throw Error('读取分类失败');return r.json();}).then(rows=>{
    stockCategories=rows;LogicalCategories.options($('#operationCategoryFilter'),rows);}).catch(e=>$('#stockOperationsNotice').text(e.message));
  $('#batchInboundWarehouse').on('change',()=>{stockPreviewToken++;stockCategoriesForWarehouse();$('#batchInboundPreviewBody').empty();});
  $('#stockOperationCategory,#batchInboundText,#stockOperationCost').on('change input',()=>{stockPreviewToken++;$('#stockExecuteConfirm').prop('checked',false);$('#batchInboundPreviewBody').empty();$('#batchInboundSummary').text('内容已变化，请重新预览');});
  $('#operationWarehouseFilter').on('change',()=>{const wid=$('#operationWarehouseFilter').val();const select=$('#operationCategoryFilter').empty().append($('<option>').val('').text('全部分类'));LogicalCategories.options(select,stockCategories,wid);loadStockOperations();});
  $(document).on('change','#operationCategoryFilter,#operationKindFilter,#operationStartFilter,#operationEndFilter,#operationStartFilterTime,#operationEndFilterTime,#operationSearchFilter',loadStockOperations);
  stockRequest('/access').then(access=>{stockCsrf=access.csrf;stockCanManage=access.can_manage;loadStockOperations();loadHistoricalCosts();}).catch(e=>$('#stockOperationsNotice').text(e.message));
});

async function loadHistoricalCosts(){
  try{
    const rows=await stockRequest('/historical-costs');const box=$('#historicalCostsList').empty();
    $('#historicalCostsNotice').text(rows.length?'以下库存必须按真实凭据补录，禁止用参考价代替。':'没有待补录历史库存');
    rows.forEach(row=>{
      const card=$('<div>').addClass('border rounded p-2 my-2');
      card.append($('<strong>').text([row.warehouse_name,row.category_name,row.product_name].join(' → ')));
      const unit=stockOperationUnit(row);
      card.append($('<p>').text('来源 '+row.source_state+' / 原批次 '+(row.source_batch_id||'无')+' · 在库未知 '+row.unknown_on_hand+unit+' / 已出库待确认 '+row.unknown_consumed+unit+' / 本次待补录总量 '+row.required_quantity+unit+stockUnitProvenance(row)+' · COST_INCOMPLETE'));
      if(!row.quantity_aligned)card.append($('<p>').addClass('text-danger').text('库存与成本层数量不一致，必须先核查，禁止直接确认补录完成。'));
      const button=$('<button>').addClass('btn btn-outline-primary btn-sm').text('补录真实历史成本').prop('disabled',!stockCanManage||!row.quantity_aligned);
      button.on('click',()=>openHistoricalCost(row));card.append(button);box.append(card);
    });
  }catch(e){$('#historicalCostsNotice').text(e.message);}
}

let historicalCostTarget=null, cancellationTarget=null, costRowSequence=0, stockModalBusy=false;
function addHistoricalCostRow(){
  const id=++costRowSequence, row=$('<div>').addClass('row g-2 my-2 cost-batch-row');
  const quantity=$('<input>').attr({id:'costQuantity'+id,type:'number',min:1,step:1}).addClass('form-control cost-quantity');
  const cost=$('<input>').attr({id:'costUnit'+id,type:'number',min:0,step:'0.01'}).addClass('form-control cost-unit');
  row.append($('<div>').addClass('col-5').append($('<label>').attr('for','costQuantity'+id).text('批次数量'),quantity),
    $('<div>').addClass('col-5').append($('<label>').attr('for','costUnit'+id).text('实际单位成本'),cost),
    $('<div>').addClass('col-2').append($('<button>').attr('type','button').addClass('btn btn-outline-danger mt-4').text('删除').on('click',()=>{row.remove();validateHistoricalCost();})));
  quantity.add(cost).on('input',()=>{$('#historicalCostConfirm').prop('checked',false);validateHistoricalCost();});
  $('#historicalCostRows').append(row);validateHistoricalCost();
}
function historicalCostBatches(){return $('.cost-batch-row').toArray().map(row=>({quantity:Number($(row).find('.cost-quantity').val()),unit_cost:Number($(row).find('.cost-unit').val())}));}
function validateHistoricalCost(){
  const batches=historicalCostBatches(), required=historicalCostTarget?Number(historicalCostTarget.required_quantity):0;
  const sum=batches.reduce((total,row)=>total+row.quantity,0);
  const valid=batches.length>0&&batches.every(row=>Number.isSafeInteger(row.quantity)&&row.quantity>0&&Number.isFinite(row.unit_cost)&&row.unit_cost>=0)
    &&$('.cost-unit').toArray().every(input=>input.value.trim()!=='')&&sum===required;
  const unit=stockOperationUnit(historicalCostTarget||{});
  $('#historicalCostBalance').text('已填 '+sum+unit+' / 待补录 '+required+unit+' / 尚差 '+(required-sum)+unit+(valid?' · 数量守恒':' · 数量不守恒或成本无效'));
  $('#submitHistoricalCost').prop('disabled',stockModalBusy||!stockCanManage||!valid||!$('#historicalCostEvidence').val().trim()||!$('#historicalCostConfirm').prop('checked'));
}
function openHistoricalCost(row){
  historicalCostTarget=row;stockModalBusy=false;
  $('#historicalCostIdentity').text([row.warehouse_name,row.category_name,row.product_name,row.source_state,'原批次 '+(row.source_batch_id||'无')].join(' → '));
  const unit=stockOperationUnit(row);
  $('#historicalCostQuantity').text('在库未知 '+row.unknown_on_hand+unit+' / 已出库待确认 '+row.unknown_consumed+unit+' / 待补录 '+row.required_quantity+unit+stockUnitProvenance(row));
  $('#historicalCostRows').empty();$('#historicalCostEvidence').val('');$('#historicalCostConfirm').prop('checked',false);$('#historicalCostError').text('');
  addHistoricalCostRow();$('#historicalCostModal').modal('show');
}
function openStockCancellation(row){
  cancellationTarget=row;stockModalBusy=false;const box=$('#cancelStockSummary').empty();
  const warehouse=warehouses.find(item=>Number(item.id)===Number(row.warehouse_id));
  const stockCategory=stockRows.find(item=>Number(item.parent_id)===Number(row.category_id));
  const category=stockCategories.find(item=>Number(item.id)===Number(row.category_id))||(stockCategory?{name:stockCategory.parent_name}:null);
  box.append($('<p>').text('批次 '+row.id+' / '+(warehouse?warehouse.name:'仓库')+' (#'+row.warehouse_id+') / '+(category?category.name:'分类')+' (#'+row.category_id+') / '+row.lines.length+' 种商品 / '+stockQuantitySummary(row.lines)));
  row.lines.forEach(line=>{const product=products.find(item=>Number(item.id)===Number(line.product_id)),unit=stockOperationUnit(line);box.append($('<p>').text((product?product.name:'商品')+' (#'+line.product_id+') · '+line.quantity+unit+' · 撤销库存影响 '+(row.operation_type==='inbound'?'-':'+')+line.quantity+unit+stockUnitProvenance(line)));});
  $('#cancelStockReason').val('');$('#cancelStockConfirm').prop('checked',false);$('#cancelStockError').text('');$('#submitCancelStock').prop('disabled',true);$('#cancelStockModal').modal('show');
}
$(function(){
  $('#addHistoricalCostRow').on('click',addHistoricalCostRow);
  $('#historicalCostEvidence').on('input',()=>{$('#historicalCostConfirm').prop('checked',false);validateHistoricalCost();});
  $('#historicalCostConfirm').on('change',validateHistoricalCost);
  $('#submitHistoricalCost').on('click',async function(){
    if($(this).prop('disabled'))return;
    stockModalBusy=true;validateHistoricalCost();
    try{const row=historicalCostTarget;await stockRequest('/historical-costs',{warehouse_id:row.warehouse_id,category_id:row.category_id,product_id:row.product_id,layer_id:row.layer_id,batches:historicalCostBatches(),evidence:$('#historicalCostEvidence').val().trim()});
      $('#historicalCostModal').modal('hide');await loadHistoricalCosts();loadAll();await loadStockOperations();
    }catch(e){$('#historicalCostError').text(e.message);}finally{stockModalBusy=false;validateHistoricalCost();}
  });
  $('#cancelStockReason,#cancelStockConfirm').on('input change',()=>$('#submitCancelStock').prop('disabled',stockModalBusy||!$('#cancelStockReason').val().trim()||!$('#cancelStockConfirm').prop('checked')));
  $('#submitCancelStock').on('click',async function(){
    if($(this).prop('disabled'))return;stockModalBusy=true;$(this).prop('disabled',true);
    try{await stockRequest('/'+encodeURIComponent(cancellationTarget.id)+'/cancel',{reason:$('#cancelStockReason').val().trim()});
      $('#cancelStockModal').modal('hide');loadAll();await loadStockOperations();await loadHistoricalCosts();
    }catch(e){$('#cancelStockError').text(e.message);}finally{stockModalBusy=false;$('#submitCancelStock').prop('disabled',!stockCanManage);}
  });
});
