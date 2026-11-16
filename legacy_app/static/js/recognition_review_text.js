/* Display only. Never interprets customer/model text as markup or rules. */
(function(scope){
  'use strict';
  const present=v=>v!==null&&v!==undefined&&v!=='';
  const text=v=>typeof v==='string'||typeof v==='number'?String(v):'';
  const codes={TIMEOUT:'AI请求超时，未取得有效分析结论',ANALYSIS_FAILED:'AI分析失败，请人工核对',
    INTERRUPTED_OUTCOME_UNKNOWN:'上次分析意外中断，结果不确定，未自动重试',
    UNKNOWN_VALIDATION:'校验未通过，请人工核对原单',LOCAL_ADMIN:'管理员填写',DEEPSEEK:'DeepSeek分析建议'};
  function reasons(value,empty='未记录到错误原因。'){
    if(!present(value)||(Array.isArray(value)&&!value.length))return empty;
    if(Array.isArray(value))return value.map(v=>reasons(v,'')).filter(Boolean).join('\n')||empty;
    if(typeof value==='object')return reasons(value.message||value.reason||value.error||value.code,empty);
    const s=text(value);return codes[s]||(/[\u4e00-\u9fff]/.test(s)?s:'校验未通过，需人工核对；技术信息已留存后台。');
  }
  function item(value){
    let label,qty;
    if(Array.isArray(value)){label=text(value[0]);qty=value[1];}
    else if(value&&typeof value==='object'){
      label=[value.category||value.parent_name,text(value.flavor||value.product_name||value.name||value.item)].filter(Boolean).join(' / ');
      qty=value.quantity;
    }else return text(value);
    label=label.replace(/^品类\(([^()]*)\)口味\(([^()]*)\)$/,'$1 / $2');
    return label+(present(qty)?' × '+text(qty):'（数量未留存）');
  }
  function result(value){
    if(!value||typeof value!=='object')return '未留存该阶段的识别结果，不能据此判断是否调用过AI。';
    const lines=[];
    for(const [label,v] of [['收件人',value.recipient||value.recipient_name],['手机号',value.phone||value.recipient_phone],['地址',value.address||value.recipient_address]]){
      lines.push(label+'：'+(text(v)||'未识别或未留存'));
    }
    lines.push('','商品明细：');
    if(Array.isArray(value.items)&&value.items.length)lines.push(...value.items.map(v=>'• '+item(v)));
    else lines.push('未留存商品明细。');
    if(present(value.computed_total))lines.push('识别合计：'+text(value.computed_total));
    if(present(value.declared_total))lines.push('原单填写合计：'+text(value.declared_total));
    for(const [label,vs] of [['履约备注',value.fulfillment_notes||value.notes],['备选口味',value.alternates||value.ai_alternates]]){
      if(Array.isArray(vs)&&vs.length)lines.push(label+'：'+vs.map(v=>typeof v==='string'?v:item(v)).join('；'));
    }
    if(Array.isArray(value.section_subtotals)&&value.section_subtotals.length){
      lines.push('分类小计：'+value.section_subtotals.map(v=>text(v.section||v.category||v.name)+' '+text(v.quantity??v.total??v.count)).join('；'));
    }
    return lines.join('\n');
  }
  function suggestions(values){
    if(!Array.isArray(values)||!values.length)return '暂无叫法建议。';
    return values.map(v=>{
      const kind=v.kind==='MODEL_ALIAS'?'商品／分类名称':'口味';
      const label=v.standard||v.flavor||v.target||('目录商品 '+text(v.product_id));
      return '• '+kind+'：'+text(v.alias||v.raw_flavor)+' → '+text(label)+(v.category?'（分类：'+text(v.category)+'）':'');
    }).join('\n');
  }
  function analysis(value){
    if(!value)return '暂无DeepSeek分析结论。';
    if(typeof value==='string')return value;
    const lines=[];
    if(value.reason)lines.push('原因：'+text(value.reason));
    if(value.advice)lines.push('建议：'+text(value.advice));
    if(Array.isArray(value.proposals)&&value.proposals.length)lines.push(suggestions(value.proposals));
    return lines.join('\n')||'未留存可展示的文字结论。';
  }
  const api={result,reasons,suggestions,analysis};
  scope.RecognitionReviewText=api;
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
})(typeof window!=='undefined'?window:globalThis);
