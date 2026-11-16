let brandData={brands:[],candidates:[]},brandTarget=null,brandAction=null,brandBatchPlan=null;
const selectedFlavorCandidates=new Set();
function filteredCandidates(){
 const q=String($('#candidateSearch').val()||'').trim().toLowerCase(),status=$('#candidateStatus').val()||'';
 return sortBrandCandidates(brandData.candidates).filter(c=>(!status||c.status===status)&&(!q||[c.alias,c.standard,c.category,c.type].join(' ').toLowerCase().includes(q)));
}
async function reviewSelectedFlavors(action){
 const ids=[...selectedFlavorCandidates];if(!brandCanManage||!ids.length)return;
 const controls=$('#candidateBulkControls :input');controls.prop('disabled',true);
 try{
  const body={ids,action};const preview=await brandRequest('/candidates/batch/preview',body);
  const description=action==='approve'?'通过这些口味候选，全部重新校验目录及Alias冲突':'删除候选记录（不会删除正式Alias）';
  const previewSkipped=(preview.skipped||[]).map(row=>row.id+'：'+row.reason).join('；');
  if(!confirm(`确认${description}？共 ${preview.count} 条；可处理 ${preview.eligible} 条；冲突保留 ${preview.skipped.length} 条。${previewSkipped?'\n'+previewSkipped:''}`))return;
  const result=await brandRequest('/candidates/batch/commit',{...body,token:preview.token});
  const skipped=(result.skipped||[]).map(row=>row.id+'：'+row.reason).join('；');
  selectedFlavorCandidates.clear();await loadBrands();$('#brandNotice').text(`已处理 ${result.processed} 条口味候选；冲突保留 ${result.skipped.length} 条${skipped?'：'+skipped:''}`);
 }catch(error){$('#brandNotice').text(error.message);}
 finally{controls.prop('disabled',false);renderBrands();}
}
const brandStatus={active:'已启用',pending:'待审核',disabled:'已停用',conflict:'冲突 / 待人工判断',approved:'已通过',ignored:'已忽略',rejected:'已拒绝'};
function candidatePriority(status){
 if(['ignored','rejected','conflict','failed'].includes(status))return 0;
 if(['active','approved','enabled'].includes(status))return 2;
 return 1;
}
function sortBrandCandidates(rows){
 return [...rows].sort((a,b)=>candidatePriority(a.status)-candidatePriority(b.status)||
  (Date.parse(b.last_seen)||0)-(Date.parse(a.last_seen)||0)||String(a.id).localeCompare(String(b.id)));
}
function brandBusinessTime(value){
 return RainbowBusinessTime.format(value);
}
function bulkControls(box,rows,payload,refresh){
 const editable=rows.filter(a=>!a.readonly),manual=editable.filter(a=>String(a.source||'').startsWith('人工'));
 const count=status=>manual.filter(a=>a.status===status).length;
 const activeTotal=editable.filter(a=>a.status==='active').length,reviewedActive=editable.filter(a=>a.status==='active'&&!String(a.source||'').startsWith('人工')).length;
 box.empty().append($('<span>').text('实际生效 '+activeTotal+' ｜ 人工维护已启用 '+count('active')+' ｜ 审核来源已生效 '+reviewedActive+' ｜ 待审核 '+count('pending')+' ｜ 已停用 '+count('disabled')+' '));
 ['approve','disable'].forEach(action=>{
  const n=count(action==='approve'?'pending':'active'),label=action==='approve'?'一键全部启用':'一键全部停用';
  box.append($('<button>').addClass('btn btn-outline-primary btn-sm me-2').text(label).prop('disabled',!brandCanManage||!n).on('click',()=>{
   const input=payload.product_id?$('#flavorAliasInput'):$('#brandAliasInput');
   const saved=rows.filter(a=>!a.readonly).map(a=>a.alias).join('\n');
   if(input.val()!==saved){(payload.product_id?$('#flavorError'):$('#brandEditorError')).text('存在未保存修改，请先保存；不会丢弃输入。');return;}
   brandAction={path:'/aliases/batch/status',body:{...payload,action},refresh};
   $('#brandActionDescription').text('确定'+(action==='approve'?'启用当前对象全部待审核':'停用当前对象全部已启用人工')+'叫法？ '+(action==='approve'?'待审核：':'已启用：')+n+'条。启用前将重新执行冲突校验；不影响其他对象。');
   $('#brandActionError').text('');$('#brandActionSubmit').text(action==='approve'?'确认全部启用':'确认全部停用');$('#brandActionModal').modal('show');
  }));
 });
}
async function brandRequest(path,body){const r=await fetch('/api/brands'+path,body?{method:'POST',headers:{'Content-Type':'application/json','X-Rules-CSRF':brandCsrf},body:JSON.stringify(body)}:{cache:'no-store'});const data=await r.json();if(!r.ok)throw Error(data.detail||'操作失败');return data;}
function brandButton(label,path,body,row){return $('<button>').addClass('btn btn-outline-primary btn-sm me-1').text(label).prop('disabled',!brandCanManage).on('click',()=>{brandAction={path,body};$('#brandActionDescription').text(label+'：'+row.alias+' → '+row.standard+'（只修改叫法关系，不修改品牌、商品或订单）');$('#brandActionError').text('');$('#brandActionModal').modal('show');});}
function renderBrands(){
 $('#candidateSelectionCount').text('已选 '+selectedFlavorCandidates.size+' 条');
 const wid=$('#brandWarehouse').val(),q=$('#brandSearch').val().trim().toLowerCase(),box=$('#brandRows').empty();
 brandData.brands.filter(b=>(!wid||b.type==='bundle'||(b.warehouses||[{id:b.warehouse_id}]).some(w=>String(w.id)===wid))&&(!q||[b.standard,...b.aliases.map(a=>a.alias)].join(' ').toLowerCase().includes(q))).forEach(b=>{
  if(b.type==='bundle'){
   const tr=$('<tr>');tr.append($('<td>').text('福袋 · '+b.standard),$('<td>').text(b.aliases.filter(a=>a.status==='active').map(a=>a.alias).join('、')||'—'),$('<td>').text('福袋ID '+b.bundle_id),$('<td>').text(b.sellable?'可售':'未启用 / 不可售'),$('<td>').append($('<button>').addClass('btn btn-outline-primary btn-sm').text('维护叫法').prop('disabled',!brandCanManage).on('click',()=>openBundleAliases(b))));box.append(tr);return;
  }
  const aliases=$('<td>'),active=b.aliases.filter(a=>a.status==='active');if(!active.length)aliases.text('—');else active.forEach(a=>aliases.append($('<span>').addClass('brand-alias-tag').text(a.alias)));
  const tr=$('<tr>');tr.append($('<td>').text(b.standard),aliases,$('<td>').text('全后台'));
  const details=$('<details>').attr('data-brand-group',b.category_id).prop('open',expandedFlavorGroups.has(b.category_id)).on('toggle',function(){if(this.open)expandedFlavorGroups.add(b.category_id);else expandedFlavorGroups.delete(b.category_id);}).append($('<summary>').text(b.products.length+'个商品'));
  const flavors=$('<table>').addClass('table table-sm').append($('<thead>').append($('<tr>').append($('<th>').text('标准口味'),$('<th>').text('其他叫法'),$('<th>').text('状态'),$('<th>').text('维护'))));
  (b.flavors||[]).forEach((f,index)=>flavors.append($('<tr>').append($('<td>').text(f.standard),$('<td>').text(f.aliases.map(a=>a.alias).join('、')||'—'),$('<td>').text(f.aliases.map(a=>brandStatus[a.status]).join('、')||'未维护'),$('<td>').append($('<button>').addClass('btn btn-sm btn-outline-primary').text('维护叫法').prop('disabled',!brandCanManage).on('click',()=>openFlavor(b,index))))));details.append(flavors);tr.append($('<td>').append(details),$('<td>').append($('<button>').addClass('btn btn-outline-primary btn-sm').text('维护叫法').on('click',()=>openBrand(b))));box.append(tr);
 });
 const candidates=$('#brandCandidates').empty();filteredCandidates().forEach(c=>{
  const group=brandData.brands.find(b=>b.category_id===c.category_id&&b.warehouse_id===c.warehouse_id),tr=$('<tr>');
  const isFlavor=c.type==='EXPLICIT_FLAVOR';
  tr.append($('<td>').append($('<input>').attr({type:'checkbox','aria-label':'选择此口味候选'}).prop('checked',selectedFlavorCandidates.has(c.id)).prop('disabled',!brandCanManage||!isFlavor).on('change',function(){if(this.checked)selectedFlavorCandidates.add(c.id);else selectedFlavorCandidates.delete(c.id);$('#candidateSelectionCount').text('已选 '+selectedFlavorCandidates.size+' 条');})));
  [isFlavor?'口味叫法':'品牌叫法',c.alias,isFlavor?(c.category||'目标待确认'):c.standard,isFlavor?c.standard:'—',c.count,brandBusinessTime(c.first_seen)+' / '+brandBusinessTime(c.last_seen),'全后台',brandStatus[c.status]||c.status].forEach(v=>tr.append($('<td>').text(v)));
  if(c.review_note)tr.children().last().append($('<div>').addClass('small text-muted').text(c.review_note));
  const actions=$('<td>');if(['pending','conflict'].includes(c.status))actions.append(brandButton('通过','/candidates',{action:'approve',id:c.id},c),brandButton(isFlavor?'拒绝':'忽略','/candidates',{action:isFlavor?'reject':'ignore',id:c.id},c));if(isFlavor&&['ignored','rejected'].includes(c.status))actions.append(brandButton('撤销拒绝并恢复审核','/candidates',{action:'restore',id:c.id},c));if(c.status==='approved')actions.append(brandButton('撤销本次审核','/candidates',{action:'revoke',id:c.id},c));if(c.review_history?.length)actions.append($('<details>').append($('<summary>').text('审核版本 '+c.review_version),$('<pre>').text(JSON.stringify(c.review_history,null,2))));actions.append(brandButton('删除候选','/candidates',{action:'delete',id:c.id},c));tr.append(actions);candidates.append(tr);
 });$('#brandHistoricalCandidates').text((brandData.historical_candidates||[]).map(c=>'历史审计候选 '+c.alias+' → '+c.standard+'：继续待审核，不自动启用').join('；'));
}
function openBrand(b){brandTarget=b;brandBatchPlan=null;$('#brandEditorTitle').text('维护叫法：'+b.standard);$('#brandStandardName').val(b.standard);$('#brandEditorScope').text('全后台 / '+b.standard);$('#brandAliasInput').val(b.aliases.filter(a=>!a.readonly).map(a=>a.alias).join('\n'));$('#brandBatchPreview').prop('hidden',true).empty();$('#brandAliasSave').prop('disabled',true);$('#brandEditorError').text('');const box=$('#brandExisting').empty();
 b.aliases.forEach(a=>{const line=$('<div>').addClass('d-flex gap-2 align-items-center my-2').append($('<span>').text(a.alias+' · '+(brandStatus[a.status]||a.status)+' · '+a.source));
 if(!a.readonly){line.append(brandButton('启用','/aliases',{action:'approve',id:a.id},a),brandButton('停用','/aliases',{action:'disable',id:a.id},a),brandButton('删除','/aliases',{action:'delete',id:a.id},a));}else line.append($('<small>').text('只读'));box.append(line);});$('#brandEditorModal').modal('show');}
const originalOpenBrand=openBrand;
openBrand=function(b){originalOpenBrand(b);let box=$('#brandBulkControls');if(!box.length)box=$('<div>').attr('id','brandBulkControls').addClass('mb-2').insertAfter('#brandEditorScope');bulkControls(box,b.aliases,{warehouse_id:b.warehouse_id,category_id:b.category_id},()=>openBrand(brandData.brands.find(x=>x.warehouse_id===b.warehouse_id&&x.category_id===b.category_id)));};
async function loadBrands(){brandData=await brandRequest('');const current=$('#brandWarehouse').val(),select=$('#brandWarehouse').empty().append($('<option>').val('').text('全部仓库')),seen=new Set();brandData.brands.forEach(b=>(b.warehouses||[{id:b.warehouse_id,name:b.warehouse_name}]).forEach(w=>{if(!seen.has(w.id)){seen.add(w.id);select.append($('<option>').val(w.id).text(w.name));}}));select.val(current);renderBrands();}
$(function(){
 const controls=$('<div>').attr('id','candidateBulkControls').addClass('d-flex flex-wrap gap-2 mb-3');
 controls.append($('<input>').attr({id:'candidateSearch',type:'search',placeholder:'搜索叫法 / 分类 / 口味','aria-label':'搜索候选'}).addClass('form-control').css('max-width','280px'));
 const statuses=$('<select>').attr({id:'candidateStatus','aria-label':'候选状态'}).addClass('form-select').css('max-width','180px');
 [['','全部状态'],['pending','待审核'],['conflict','冲突'],['approved','已通过'],['ignored','已忽略'],['rejected','已拒绝']].forEach(([value,label])=>statuses.append($('<option>').val(value).text(label)));controls.append(statuses);
 controls.append($('<button>').attr('type','button').addClass('btn btn-outline-secondary').text('全选当前筛选口味').prop('disabled',!brandCanManage).on('click',()=>{filteredCandidates().filter(c=>c.type==='EXPLICIT_FLAVOR').forEach(c=>selectedFlavorCandidates.add(c.id));renderBrands();$('#candidateSelectionCount').text('已选 '+selectedFlavorCandidates.size+' 条');}));
 controls.append($('<button>').attr('type','button').addClass('btn btn-outline-secondary').text('清除选择').on('click',()=>{selectedFlavorCandidates.clear();renderBrands();$('#candidateSelectionCount').text('已选 0 条');}));
 [['approve','批量通过'],['delete','批量删除候选']].forEach(([action,label])=>controls.append($('<button>').attr('type','button').addClass('btn btn-outline-primary').text(label).prop('disabled',!brandCanManage).on('click',()=>reviewSelectedFlavors(action))));
 controls.append($('<span>').attr({id:'candidateSelectionCount',role:'status'}).text('已选 0 条'));
 const table=$('#brandCandidates').closest('.table-responsive');controls.insertBefore(table);table.find('thead tr').prepend($('<th>').text('选择'));
 $('#candidateSearch,#candidateStatus').on('input change',()=>{selectedFlavorCandidates.clear();$('#candidateSelectionCount').text('已选 0 条');renderBrands();});
 loadBrands().catch(e=>$('#brandNotice').text(e.message));$('#brandWarehouse,#brandSearch').on('change input',renderBrands);
 $('#brandAliasInput').on('input',()=>{brandBatchPlan=null;$('#brandAliasSave').prop('disabled',true);$('#brandBatchPreview').prop('hidden',true);});
 $('#brandAliasPreview').on('click',async()=>{if(!brandTarget||!brandCanManage)return;brandBatchPlan=null;$('#brandAliasSave').prop('disabled',true);$('#brandEditorError').text('');try{const plan=await brandRequest('/aliases/batch/preview',{warehouse_id:brandTarget.warehouse_id,category_id:brandTarget.category_id,aliases:$('#brandAliasInput').val()});brandBatchPlan=plan;const box=$('#brandBatchPreview').prop('hidden',false).empty();box.append($('<p>').text('标准名称：'+plan.standard+' / 新增 '+plan.new.length+' / 已有 '+plan.existing.length+' / 去重 '+plan.duplicates+' / 冲突 '+plan.conflicts.length));plan.aliases.forEach(a=>box.append($('<div>').text(a)));plan.conflicts.forEach(c=>box.append($('<div>').addClass('text-danger').text(c.alias+'：'+c.reason)));$('#brandAliasSave').prop('disabled',!!plan.conflicts.length);}catch(e){$('#brandEditorError').text(e.message);}});
 $('#brandAliasSave').on('click',async()=>{if(!brandTarget||!brandCanManage||!brandBatchPlan)return;$('#brandAliasSave').prop('disabled',true);try{await brandRequest('/aliases/batch/save',{warehouse_id:brandTarget.warehouse_id,category_id:brandTarget.category_id,aliases:$('#brandAliasInput').val(),token:brandBatchPlan.token});await loadBrands();openBrand(brandData.brands.find(b=>b.category_id===brandTarget.category_id&&b.warehouse_id===brandTarget.warehouse_id));}catch(e){$('#brandEditorError').text(e.message);brandBatchPlan=null;}});
 $('#brandActionSubmit').on('click',async()=>{if(!brandAction)return;const operation=brandAction;$('#brandActionSubmit').prop('disabled',true);try{const result=await brandRequest(operation.path,operation.body);$('#brandActionModal').modal('hide');if(!operation.refresh)$('#brandEditorModal').modal('hide');await loadBrands();if(operation.refresh){operation.refresh();const message='批量操作完成：成功 '+result.success+' ｜ 冲突保留 '+result.conflicts.length+' ｜ 失败 '+result.failed+(result.conflicts.length?'；'+result.conflicts.map(c=>c.alias+'：'+c.reason).join('；'):'');(operation.body.product_id?$('#flavorError'):$('#brandEditorError')).text(message);}}catch(e){$('#brandActionError').text(e.message);}finally{$('#brandActionSubmit').prop('disabled',false).text('确认');}});
});
