let warehouseShipping={rules:{},categories:[],warehouses:[]}, shippingTarget=null;
function filterSelectedWarehouse(){
  const wid=$('#shippingWarehouse').val();
  $('#salesPauseRows tr').each(function(){const name=$(this).find('td:first').text();const wh=warehouseShipping.warehouses.find(w=>w.name===name);$(this).toggle(!wid||!!wh&&String(wh.id)===wid);});
  $('#splitWarehouseFilter').val(wid||'all').trigger('change');
  $('#btnCloseAllWarehouse,#btnOpenAllWarehouse').toggle(!wid);
}
function renderWarehouseShipping(){
 const wid=$('#shippingWarehouse').val(),box=$('#warehouseShippingRows').empty();
 warehouseShipping.categories.filter(c=>String(c.warehouse_id)===wid).forEach(c=>{
   const rule=warehouseShipping.rules[wid+':'+c.id],tr=$('<tr>');
   tr.append($('<td>').text(c.name),$('<td>').text(rule?'¥'+rule.fee:'使用现有默认运费'),$('<td>').text(rule?'每 '+rule.units+' 件一段':'兼容现有规则'));
   const button=$('<button>').addClass('btn btn-outline-primary btn-sm').text('设置').on('click',()=>{
     shippingTarget=c;const wh=warehouseShipping.warehouses.find(w=>String(w.id)===wid);
     $('#warehouseShippingIdentity').text(wh.name+' → '+c.name);$('#warehouseShippingFee').val(rule?rule.fee:'');$('#warehouseShippingUnits').val(rule?rule.units:1);
     $('#warehouseShippingError').text('');$('#warehouseShippingModal').modal('show');
   });tr.append($('<td>').append(button));box.append(tr);
 });filterSelectedWarehouse();
}
async function loadWarehouseShipping(){
 const response=await fetch('/api/settings/warehouse-shipping',{cache:'no-store'}),data=await response.json();
 if(!response.ok)throw Error(data.detail||'读取运费失败');warehouseShipping=data;
 const selected=$('#shippingWarehouse').val(),select=$('#shippingWarehouse').empty().append($('<option>').val('').text('请选择仓库'));
 data.warehouses.forEach(w=>select.append($('<option>').val(w.id).text(w.name+(w.is_active?'':'（停用）'))));select.val(selected);
 $('#warehouseShippingNotice').text(data.fallback);renderWarehouseShipping();
}
async function saveWarehouseShipping(action){
 if(!shippingTarget)return;$('#warehouseShippingSave,#warehouseShippingReset').prop('disabled',true);
 try{const response=await fetch('/api/settings/warehouse-shipping',{method:'POST',headers:{'Content-Type':'application/json','X-Shipping-CSRF':warehouseShipping.csrf},
 body:JSON.stringify({warehouse_id:shippingTarget.warehouse_id,category_id:shippingTarget.id,fee:action==='reset'?0:$('#warehouseShippingFee').val(),units:action==='reset'?1:Number($('#warehouseShippingUnits').val()),action})});
 const result=await response.json();if(!response.ok)throw Error(result.detail||'保存失败');await loadWarehouseShipping();$('#warehouseShippingModal').modal('hide');
 }catch(e){$('#warehouseShippingError').text(e.message);}finally{$('#warehouseShippingSave,#warehouseShippingReset').prop('disabled',false);}
}
$(function(){loadWarehouseShipping().catch(e=>$('#warehouseShippingNotice').text(e.message));$('#shippingWarehouse').on('change',renderWarehouseShipping);
 $('#warehouseShippingSave').on('click',()=>saveWarehouseShipping('save'));$('#warehouseShippingReset').on('click',()=>saveWarehouseShipping('reset'));
});
