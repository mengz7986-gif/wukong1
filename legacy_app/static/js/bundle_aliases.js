let bundleAliasTarget=null,bundleAliasBusy=false;
function openBundleAliases(bundle){
 bundleAliasTarget=bundle;
 let modal=$('#bundleAliasModal');
 if(!modal.length){
  modal=$('<div>').attr({id:'bundleAliasModal',tabindex:'-1'}).addClass('modal fade');
  const content=$('<div>').addClass('modal-content'),body=$('<div>').addClass('modal-body');
  content.append($('<div>').addClass('modal-header').append($('<h5>').attr('id','bundleAliasTitle'),$('<button>').addClass('btn-close').attr({'data-bs-dismiss':'modal','aria-label':'关闭'})));
  body.append($('<div>').attr('id','bundleAliasRows'),$('<label>').attr('for','bundleAliasNew').text('新增叫法（一行一个，需审核后生效）'),$('<textarea>').attr({id:'bundleAliasNew',rows:4,maxlength:20000}).addClass('form-control'),$('<p>').attr({id:'bundleAliasError',role:'alert'}).addClass('text-danger'));
  content.append(body,$('<div>').addClass('modal-footer').append($('<button>').addClass('btn btn-secondary').attr('data-bs-dismiss','modal').text('关闭'),$('<button>').addClass('btn btn-primary').text('保存新增叫法（待审核）').on('click',()=>bundleAliasOperation({action:'create',aliases:$('#bundleAliasNew').val()}))));
  modal.append($('<div>').addClass('modal-dialog modal-lg').append(content));$('body').append(modal);
 }
 $('#bundleAliasTitle').text('福袋叫法：'+bundle.standard+' · ID '+bundle.bundle_id);
 $('#bundleAliasNew').val('');$('#bundleAliasError').text('');const box=$('#bundleAliasRows').empty();
 bundle.aliases.forEach(row=>{
  const line=$('<div>').addClass('border rounded p-2 mb-2'),input=$('<input>').addClass('form-control mb-2').attr('maxlength',40).val(row.alias);
  line.append($('<p>').text((brandStatus[row.status]||row.status)+' · '+row.source),input);
  [['保存修改（重新审核）','edit'],['审核启用','approve'],['停用','disable'],['删除叫法','delete']].forEach(([label,action])=>line.append($('<button>').addClass('btn btn-sm btn-outline-primary me-1').text(label).prop('disabled',!brandCanManage).on('click',()=>{
   if(!confirm(label+'？只修改福袋叫法，不修改库存、成本、价格或历史订单。'))return;
   bundleAliasOperation({action,id:row.id,alias:input.val()});
  })));box.append(line);
 });modal.modal('show');
}
async function bundleAliasOperation(body){
 if(bundleAliasBusy||!bundleAliasTarget||!brandCanManage)return;
 bundleAliasBusy=true;$('#bundleAliasModal button,input,textarea').prop('disabled',true);$('#bundleAliasError').text('');
 const id=bundleAliasTarget.bundle_id;
 try{
  await brandRequest('/bundle-aliases',{...body,bundle_id:id});await loadBrands();
  const current=brandData.brands.find(b=>b.type==='bundle'&&b.bundle_id===id);
  if(current)openBundleAliases(current);else $('#bundleAliasModal').modal('hide');
 }catch(error){$('#bundleAliasError').text(error.message);}
 finally{bundleAliasBusy=false;$('#bundleAliasModal button,input,textarea').prop('disabled',!brandCanManage);}
}
