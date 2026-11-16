/* Learning review uses saved evidence and existing audited catalog approvals. */
(() => {
  'use strict';
  const canManage = document.getElementById('recognition-review-management')?.dataset.canManage === '1';
  const node = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text || ''; if (cls) n.className = cls; return n; };
  const button = (text, fn, cls='btn btn-outline-primary btn-sm') => { const b=node('button',text,cls);b.type='button';b.disabled=!canManage;b.onclick=action(fn);return b; };
  const left=document.getElementById('knowledge-management');
  left.replaceChildren(node('h2','商品及口味叫法','h5'),node('p','沿用原有叫法与审核方式；这里只显示名称、口味及对应叫法。','small text-muted'));
  const filters=node('div','','row g-2 mb-2');
  const search=node('input');search.className='form-control';search.placeholder='搜索客户叫法或正式商品';search.setAttribute('aria-label','搜索商品或口味');
  const category=node('select');category.className='form-select';category.setAttribute('aria-label','商品分类');
  filters.append(search,category);left.append(filters);
  const tools=node('div','','d-flex flex-wrap gap-2 mb-2');
  const all=node('input');all.type='checkbox';all.setAttribute('aria-label','全选当前筛选候选');
  const selectLabel=node('label','全选当前筛选 ');selectLabel.prepend(all);tools.append(selectLabel);
  const listing=node('div');listing.id='smartProductCandidateList';
  const message=node('p','','small');message.setAttribute('role','status');
  const selected=new Set();let rows=[],brands=[],manualPending=[];
  const manual=node('div','','border rounded p-2 mb-3');
  manual.append(node('h3','手动添加商品／口味叫法','h6'));
  const target=node('select');target.className='form-select my-2';target.setAttribute('aria-label','选择正式商品或口味');
  const aliases=node('input');aliases.className='form-control';aliases.placeholder='填写一个客户叫法，不用搬运原单';
  const feedback=node('p','','small');feedback.setAttribute('role','status');let preview=null;
  const invalidate=()=>{preview=null;save.disabled=true;};
  async function manualCall(op,body,path){const response=await fetch('/api/brands/'+path+'/'+op,{method:'POST',headers:{'Content-Type':'application/json','X-Rules-CSRF':left.dataset.rulesCsrf},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Error(data.detail||'操作失败');return data;}
  const save=button('保存为待审核',async()=>{if(!preview)throw Error('请先预览');await manualCall('save',{...preview.body,token:preview.token},preview.path);invalidate();await refresh();feedback.textContent='已保存待审核，未自动生效。';});save.disabled=true;
  manual.append(target,aliases,button('预览叫法',async()=>{const t=JSON.parse(target.value||'null');if(!t)throw Error('请选择正式目录');const body={warehouse_id:t.warehouse_id,category_id:t.category_id,aliases:aliases.value};if(t.product_id)body.product_id=t.product_id;const path=t.product_id?'flavors':'aliases/batch';const data=await manualCall('preview',body,path);preview={body,path,token:data.token};save.disabled=!canManage||!!data.conflicts?.length;feedback.textContent=`新增${data.new?.length||0}条，已有${data.existing?.length||0}条；`+(data.conflicts?.map(r=>r.alias+'：'+r.reason).join('；')||'无冲突，可保存待审核。');}),save,feedback);
  const manageTop=node('a','审核、编辑已有叫法','btn btn-outline-secondary btn-sm');manageTop.href='/admin/rules';manual.append(manageTop);left.insertBefore(manual,filters);
  target.onchange=invalidate;aliases.oninput=invalidate;
  const existing=node('div','','mt-3');existing.id='smartExistingAliases';
  const filtered=()=>rows.filter(r=>['pending','conflict'].includes(r.status)&&(!category.value||(r.category||r.standard)===category.value)&&(`${r.alias} ${r.standard} ${r.category||''}`).toLowerCase().includes(search.value.toLowerCase()));
  async function reviewProducts(ids,op){
    if(!ids.length)throw Error('请先勾选候选');
    if(!window.confirm(`${op==='approve'?'通过':'拒绝'}所选${ids.length}条候选？每条分别校验正式目录及叫法冲突。`))return;
    const result=await call('/api/ai/product-candidates/review',{ids,action:op});
    await refresh();message.textContent=result.results.map(r=>r.success?'审核完成':r.reason).join('；');
  }
  tools.append(button('批量通过',()=>reviewProducts([...selected],'approve')),button('批量拒绝',()=>reviewProducts([...selected],'reject')));left.append(tools,message,listing,existing);
  async function reviewManual(row,op){
    if(!window.confirm(`确认${op==='approve'?'通过':'拒绝'}叫法“${row.alias}”？`))return;
    const path=row.type==='EXPLICIT_FLAVOR'?'/api/brands/flavors/mutate':'/api/brands/aliases';
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Rules-CSRF':left.dataset.rulesCsrf},body:JSON.stringify({id:row.id,action:op,expected_pending:true})});
    const data=await response.json();await refresh();if(!response.ok)throw Error(data.detail||'审核失败');message.textContent=op==='approve'?'审核通过，已生效':'已拒绝，未生效';
  }
  function render(){listing.replaceChildren(node('h3','手动添加待审核','h6'));const visible=filtered();all.checked=visible.length>0&&visible.every(r=>selected.has(r.id));
    const manualRows=manualPending.filter(r=>(!category.value||r.category===category.value)&&(`${r.alias} ${r.standard} ${r.category||''}`).toLowerCase().includes(search.value.toLowerCase()));
    if(!manualRows.length)listing.append(node('p','暂无手动添加的待审核叫法','text-muted'));
    for(const row of manualRows){const card=node('div','','border rounded p-2 mb-2');card.append(node('p',`${row.type==='EXPLICIT_FLAVOR'?'口味':'商品'}：${row.alias} → ${row.category||''}／${row.standard}`));const approve=button('通过',()=>reviewManual(row,'approve'));approve.disabled=!canManage||!!row.conflict_reason;card.append(approve,button('拒绝',()=>reviewManual(row,'reject'),'btn btn-outline-danger btn-sm'));if(row.conflict_reason)card.append(node('p',row.conflict_reason,'text-danger small'));listing.append(card);}
    listing.append(node('h3','识别生成待审核','h6'));
    if(!visible.length)listing.append(node('p','暂无匹配的待审核候选','text-muted'));
    visible.forEach(row=>{const card=node('div','','border rounded p-2 mb-2');const tick=node('input');tick.type='checkbox';tick.checked=selected.has(row.id);tick.setAttribute('aria-label','选择叫法 '+row.alias);tick.onchange=()=>{tick.checked?selected.add(row.id):selected.delete(row.id);};
      const title=node('label',' '+row.alias);title.prepend(tick);card.append(title,node('p',`推荐：${row.category||''} ${row.standard||'尚未匹配'} · 出现${row.count||0}次`,'small my-2'));
      const rowActions=node('div','','d-flex flex-wrap gap-2');rowActions.append(button('通过',()=>reviewProducts([row.id],'approve')),button('拒绝',()=>reviewProducts([row.id],'reject'),'btn btn-outline-danger btn-sm'));card.append(rowActions);listing.append(card);});
    renderExisting();
  }
  function renderExisting(){
    existing.replaceChildren(node('h3','已有商品／口味叫法','h6'));
    const query=search.value.trim().toLowerCase();
    const statuses={active:'已启用',pending:'待审核',disabled:'已停用',inactive:'已停用',rejected:'已拒绝',withdrawn:'已撤销'};
    const matches=value=>String(value||'').toLowerCase().includes(query);
    const aliasRows=aliases=>{const box=node('div','','small');for(const alias of aliases||[])box.append(node('p',`${alias.alias} → ${alias.standard}（${statuses[alias.status]||'未启用'}）`,'mb-1'));return box;};
    let shown=0;
    for(const brand of brands){
      if(category.value&&brand.standard!==category.value)continue;
      const categoryMatch=!query||matches(brand.standard);
      const names=(brand.aliases||[]).filter(row=>categoryMatch||matches(row.alias));
      const flavors=(brand.flavors||[]).filter(flavor=>(flavor.aliases||[]).length&&(categoryMatch||matches(flavor.standard)||flavor.aliases.some(row=>matches(row.alias))));
      if(!categoryMatch&&!names.length&&!flavors.length)continue;
      const group=node('details','','border rounded p-2 mb-2');
      group.append(node('summary',brand.standard+' · '+((brand.aliases||[]).length+(brand.flavors||[]).reduce((sum,f)=>sum+(f.aliases||[]).length,0))+' 个叫法'));
      if(names.length){group.append(node('h4','商品叫法','h6 mt-2'),aliasRows(names));}
      for(const flavor of flavors)group.append(node('h4','口味：'+flavor.standard,'h6 mt-2'),aliasRows(flavor.aliases));
      if(!names.length&&!flavors.length)group.append(node('p','暂无已配置叫法','small text-muted mt-2 mb-0'));
      existing.append(group);shown++;
    }
    if(!shown)existing.append(node('p','暂无匹配叫法','text-muted'));
    const manage=node('a','管理商品和口味叫法','btn btn-outline-secondary btn-sm');manage.href='/admin/rules';existing.append(manage);
  }
  async function refresh(){
    const responses=await Promise.all([fetch('/api/ai/product-candidates',{cache:'no-store'}),fetch('/api/brands',{cache:'no-store'})]);
    if(responses.some(response=>!response.ok))throw Error('商品及口味叫法读取失败');
    rows=(await responses[0].json()).candidates||[];const catalogData=await responses[1].json();brands=catalogData.brands||[];manualPending=catalogData.manual_pending||[];selected.clear();
    target.replaceChildren(new Option('选择正式商品或口味',''));for(const b of brands){target.append(new Option('商品：'+b.standard,JSON.stringify({warehouse_id:b.warehouse_id,category_id:b.category_id})));for(const f of b.flavors||[])target.append(new Option('口味：'+b.standard+'／'+f.standard,JSON.stringify({warehouse_id:f.warehouse_id,category_id:f.category_id,product_id:f.product_id})));}invalidate();
    const old=category.value;category.replaceChildren(new Option('全部分类',''),...[...new Set([...rows.map(r=>r.category||r.standard),...brands.map(b=>b.standard)].filter(Boolean))].sort().map(v=>new Option(v,v)));category.value=old;render();
  }
  search.oninput=()=>{selected.clear();render();};category.onchange=()=>{selected.clear();render();};all.onchange=()=>{filtered().forEach(r=>all.checked?selected.add(r.id):selected.delete(r.id));render();};
  refresh().catch(e=>message.textContent=e.message);
  document.querySelector('#learning-management h2').textContent='下单习惯／小计总计及其他规则';

  function format(value){
    if(!value)return '历史识别结果未保存，无法查看。';
    const result=value.result||value;const lines=[];
    const contact=[result.recipient||result.recipient_name,result.phone||result.recipient_phone,result.address||result.recipient_address].filter(Boolean);
    if(contact.length)lines.push(contact.join('\n'),'');
    let last='';for(const row of result.items||[]){const raw=Array.isArray(row)?String(row[0]):String(row.flavor||row.product||row.product_name||row.display_name||'未识别商品');const match=raw.match(/^(?:品类|型号)\((.*?)\)\s*口味\((.*)\)$/);const group=match?match[1]:(row.category||row.parent_name||'');if(group&&group!==last){lines.push(group+'：');last=group;}const product=match?match[2]:raw;lines.push(`${product==='[未验证字段已隐藏]'?'历史商品信息未完整保存':product}×${(Array.isArray(row)?row[1]:row.quantity)??'未识别'}`);}
    if(result.fulfillment_notes?.length)lines.push('缺货备选：'+result.fulfillment_notes.filter(x=>typeof x==='string').join('；'));
    for(const item of result.alternates||result.ai_alternates||[]){if(item&&typeof item==='object')lines.push('缺货备选：'+(item.category?item.category+'／':'')+(item.for?item.for+'缺货时使用':'')+(item.flavor||'历史备选信息未完整保存'));}
    return lines.join('\n')||'历史识别结果未保存，无法查看。';
  }
  openLearningDetail=async row=>{
    try{
      const data=canManage?await call('/api/ai/learning-center/detail/authorized',{id:row.id}):await (await fetch('/api/ai/learning-center/detail?id='+encodeURIComponent(row.id),{cache:'no-store'})).json();
      const records=data.records||[];let record=records.at(-1);
      const modal=document.querySelector('#learningDetailModal .modal-body');modal.replaceChildren();
      const errors=node('p','','text-danger');errors.setAttribute('role','alert');
      const select=node('select');select.className='form-select mb-2';select.setAttribute('aria-label','选择关联订单证据');records.forEach((r,i)=>select.append(new Option((r.final_business_result?.order_numbers||[]).join('、')||'历史记录未保存订单编号',i)));select.value=String(records.length-1);if(records.length>1)modal.append(select);
      const content=node('div');modal.append(content,errors);
      function show(){content.replaceChildren();record=records[Number(select.value)]||records.at(-1);
        const blocks=[['客户原文',record?.original||record?.redacted_original||'历史原文未保存，无法查看。'],['本地识别结果',format(record?.local_snapshot||record?.local_parser_result?.result)]];
        if(record?.administrator_correction?.text)blocks.push(['管理员确认结果',record.administrator_correction.text]);
        for(const [heading,text] of blocks){content.append(node('h3',heading,'h6 mt-3'));const pre=node('pre',text,'border rounded p-2');pre.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;max-height:30vh;overflow:auto';content.append(pre);}
      }show();
      const label=node('label','可修改识别结果（姓名、电话、地址、分类、口味、数量及缺货备选）','form-label mt-3');
      const editor=node('textarea');editor.id='learningCorrectionText';editor.className='form-control';editor.rows=9;editor.placeholder='按完整订单格式填写，提交时使用本地规则重新校验';editor.disabled=!canManage;label.htmlFor=editor.id;
      const initialText=()=>{const value=record?.local_snapshot;return value&&(value.recipient||value.recipient_name)&&(value.phone||value.recipient_phone)&&(value.address||value.recipient_address)?format(value):(record?.original||'');};
      editor.value=initialText();select.onchange=()=>{show();editor.value=initialText();};modal.append(label,editor);
      const actions=node('div','','d-flex flex-wrap gap-2 mt-3');
      const version={id:row.id,revision:row.revision,review_version:row.review_version};
      const submit=async corrected=>{try{errors.textContent='';if(corrected){await call('/api/ai/learning-center/correct',{...version,commit_fingerprint:record?.commit_fingerprint,text:editor.value});}else{await call('/api/ai/learning-center/review',{...version,action:'approve'});}bootstrap.Modal.getInstance(document.getElementById('learningDetailModal')).hide();await loadKnowledgeCenter();}catch(e){errors.textContent=e.message;}};
      if(row.evidence_complete!==false)actions.append(button('识别正确',()=>submit(false)));
      actions.append(button('修改后通过',()=>submit(true)),button('识别错误',async()=>{await call('/api/ai/learning-center/review',{...version,action:'reject'});bootstrap.Modal.getInstance(document.getElementById('learningDetailModal')).hide();await loadKnowledgeCenter();},'btn btn-outline-danger'));
      if(row.evidence_complete===false)errors.textContent='证据不完整，禁止直接通过；只有可信原文与重新校验充分时才能审核修正。';
      const close=node('button','关闭','btn btn-outline-secondary');close.type='button';close.dataset.bsDismiss='modal';actions.append(close);modal.append(actions);
      bootstrap.Modal.getOrCreateInstance(document.getElementById('learningDetailModal')).show();
    }catch(e){document.getElementById('notice').textContent=e.message;}
  };
})();
