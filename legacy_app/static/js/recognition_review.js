/* Untrusted originals/model output are text, never HTML or executable rules. */
(() => {
  'use strict';
  const root = document.getElementById('recognition-review-management');
  if (!root) return;
  const canManage = root.dataset.canManage === '1';
  const get = id => document.getElementById(id);
  const notice = get('recognitionReviewStatus');
  const list = get('recognitionReviewList');
  const detail = get('recognitionReviewDetail');
  const modal = get('recognitionReviewModal');
  const dialogNotice = get('recognitionReviewDialogStatus');
  const readable = window.RecognitionReviewText;
  let page = 1, products = null, viewVersion = 0;
  const closeDetail = () => { viewVersion++; modal.close(); detail.replaceChildren(); dialogNotice.textContent=''; };
  get('recognitionReviewClose').onclick=closeDetail;
  modal.addEventListener('cancel',event=>{event.preventDefault();closeDetail();});
  modal.addEventListener('close',()=>{viewVersion++;detail.replaceChildren();dialogNotice.textContent='';});
  const node = (tag, text, cls = '') => {
    const n = document.createElement(tag); n.textContent = text; n.className = cls; return n;
  };
  const button = (text, fn, enabled = true) => {
    const b = node('button', text, 'btn btn-outline-primary btn-sm me-2 mb-2');
    b.type = 'button'; b.disabled = !enabled;
    b.onclick = async () => { b.disabled = true; try { await fn(); } catch (e) { notice.textContent = e.message; if(modal.open)dialogNotice.textContent=e.message; } finally { b.disabled = !enabled; } };
    return b;
  };
  async function api(url, body) {
    const r = await fetch('/api/ai/recognition-reviews' + url, body === undefined ?
      {cache: 'no-store'} : {method: 'POST', cache: 'no-store', headers: {'Content-Type': 'application/json', 'X-AI-CSRF': csrf}, body: JSON.stringify(body)});
    let data; try { data = await r.json(); } catch (_) { throw Error('后台响应无效，请刷新后核对状态'); }
    if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : '后台校验失败');
    return data;
  }
  async function load() {
    const data = await api('?page=' + page+'&include_archived='+get('recognitionReviewShowArchived').checked);
    notice.textContent = `当前${data.total}条记录，第${page}页；已整理或收起${data.archived_total||0}条。有效叫法见上方左侧，识别规则与习惯见右侧；新建议仍需审核。`;
    list.replaceChildren();
    get('recognitionReviewPrevious').disabled = page === 1;
    get('recognitionReviewNext').disabled = page * 25 >= data.total;
    if (!data.records.length) list.append(node('p', '暂无未整理记录；重复和无有效学习内容的原单已收起，不回放订单。'));
    const labels = {organized:'已归入上方分类',archived:'无新增有效内容／已收起',pending: '待审核', observed:'本地识别留档（不自动学习）', approved: '已写入本地词库', rejected: '已拒绝学习', withdrawn: '已撤销',
      waiting: '等待分析', running: '分析中', failed: '分析失败／需人工处理', done: '分析完成', not_needed: '本地复核',
      RECOGNIZED: '已识别（不是提交凭证）', NEEDS_CONFIRMATION: '等待客户确认建议', REJECTED: '识别未通过', FAILED: '技术失败'};
    for (const row of data.records) {
      const card = node('div', '', 'border rounded p-2 mb-2');
      card.append(node('p', `${row.id.slice(0,12)} · ${labels[row.outcome]} · ${labels[row.status]} · ${labels[row.analysis]} · ${new Date(row.created).toLocaleString()}`));
      card.append(button('查看原单／识别结果并审核（记录访问审计）', () => show(row.id), canManage)); list.append(card);
    }
  }
  async function show(id) {
    const version=++viewVersion;
    detail.replaceChildren(node('p','正在读取原单与识别详情…'));
    dialogNotice.textContent='';
    if(!modal.open)modal.showModal();
    const row = await api('/detail', {id});
    if(version!==viewVersion||!modal.open)return;
    detail.replaceChildren();
    detail.append(node('p', '仅最高管理员可查看；本次查看已记录访问审计。关闭弹窗会清除本页原文。', 'small text-muted'));
    for (const [heading, value] of [['客户原单', row.original||'原单未留存。'], ['本地识别结果', readable.result(row.local_result)],
      ['识别问题', readable.reasons(row.errors)],
      ['本地候选建议（尚未确认）', readable.suggestions(row.local_candidates)],
      ['分析状态说明', readable.reasons(row.analysis_error || row.proposal_rejections,'未记录分析错误或建议拦截。')],
      ['已保存的待审核叫法', readable.suggestions(row.suggestions)]]) {
      detail.append(node('h4', heading, 'h6 mt-3'));
      const pre = node('div', value, 'border rounded p-2');
      pre.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere;max-height:25vh;overflow:auto'; detail.append(pre);
    }
    const editable = canManage && ['pending','observed'].includes(row.status) && row.analysis !== 'running';
    const reason=!canManage?'当前账号无审核权限。':row.analysis==='running'?'本条正在分析，结束后请刷新再编辑。':
      row.status==='observed'?'这是已识别留档：可以手动补充叫法，保存后转为待审核，不会修改订单。':
      row.status==='approved'?'本条叫法已审核生效；如不再适用，请使用撤销按钮。':
      ['rejected','withdrawn'].includes(row.status)?'本条已结束审核，只读查看。':'先添加并保存叫法清单，再点击审核通过；不会修改订单。';
    detail.append(node('p',reason,'border rounded p-2 bg-light'));
    if (!products) products = (await api('/catalog')).products;
    if(version!==viewVersion||!modal.open)return;
    const choices = node('div', '', 'border rounded p-2 mt-2');
    choices.append(node('p', '选择原单中的叫法及真实目录目标，加入下方待审核清单；保存不生效。可一次审核多个名称及口味。'));
    const alias = node('input', ''); alias.className = 'form-control mb-2'; alias.placeholder = '原单逐字叫法'; alias.setAttribute('aria-label','原单叫法');
    const kind = node('select',''); kind.className = 'form-select mb-2'; kind.setAttribute('aria-label','叫法类型');
    kind.append(new Option('口味叫法（仅对应分类）','FLAVOR_ALIAS'), new Option('商品／分类名称叫法','MODEL_ALIAS'));
    const target = node('select',''); target.className = 'form-select mb-2'; target.setAttribute('aria-label','标准目录目标');
    for (const p of products) target.append(new Option(`${p.parent_name} / ${p.name} [商品ID ${p.id}]`,p.id));
    let draftSuggestions=(row.suggestions || []).map(s=>({kind:s.kind,alias:s.alias,product_id:s.product_id}));
    const editor=node('div','','mb-2');editor.setAttribute('aria-label','待审核叫法清单');
    const renderDraft=()=>{
      editor.replaceChildren();
      if(!draftSuggestions.length)editor.append(node('p','清单为空，请选择叫法和标准目标后添加。','text-muted'));
      draftSuggestions.forEach((s,index)=>{
        const p=products.find(p=>Number(p.id)===s.product_id);
        const line=node('div','','border rounded p-2 mb-2');
        line.append(node('p',(s.kind==='MODEL_ALIAS'?'商品／分类名称':'口味')+'：'+s.alias+' → '+(p?(s.kind==='MODEL_ALIAS'?p.parent_name:p.parent_name+' / '+p.name):'原目录目标已不存在，请重新选择')));
        line.append(button('移除此条',async()=>{draftSuggestions.splice(index,1);renderDraft();dialogNotice.textContent='清单已修改，尚未保存。';},editable));editor.append(line);
      });
    };
    renderDraft();
    choices.append(alias, kind, target, button('加入待审核清单', async () => {
      if(!alias.value.trim()||!target.value)throw Error('请填写原单叫法并选择标准目录目标。');
      if(draftSuggestions.some(s=>s.kind===kind.value&&s.alias===alias.value.trim()&&s.product_id===Number(target.value)))throw Error('这条叫法已在清单中。');
      draftSuggestions.push({kind: kind.value, alias: alias.value.trim(), product_id: Number(target.value)});renderDraft();dialogNotice.textContent='已加入清单，尚未保存、尚未生效。';
    }, editable), editor, button('保存／替换待审核清单（不生效）', async () => {
      if(!draftSuggestions.length)throw Error('清单为空，未保存；如不采纳请使用“拒绝这条学习建议”。');
      await api('/suggest', {id, revision: row.revision, suggestion: draftSuggestions}); await load(); if(modal.open&&version===viewVersion)await show(id);
    }, editable));
    for (const n of [alias,kind,target]) n.disabled = !editable;
    detail.append(choices);
    if(!row.suggestions?.length)detail.append(node('p','审核通过暂不可用：尚未保存有效叫法清单。','small text-muted'));
    for (const [action, label, enabled] of [['approve','审核通过并写入本地词库', editable && row.status==='pending' && !!row.suggestions?.length],
      ['reject','拒绝这条学习建议',editable && row.status==='pending'], ['withdraw','撤销本条写入的叫法',canManage && row.status === 'approved']]) {
      detail.append(button(label, async () => {
        if (!window.confirm(`${label}？不会修改或重新提交原订单。未保存的编辑不参与本次审核。`)) return;
        await api('/review',{id, revision: row.revision, action}); await load(); if(modal.open&&version===viewVersion)await show(id);
      },enabled));
    }
    detail.append(button('关闭详情并清除本页原文', async () => closeDetail()));
  }
  get('recognitionReviewRefresh').onclick = () => load().catch(e => notice.textContent = e.message);
  get('recognitionReviewShowArchived').onchange=()=>{page=1;get('recognitionReviewRefresh').click();};
  get('recognitionReviewPrevious').onclick = () => { if (page > 1) page--; get('recognitionReviewRefresh').click(); };
  get('recognitionReviewNext').onclick = () => { page++; get('recognitionReviewRefresh').click(); };
  load().catch(e => notice.textContent = e.message);
})();
