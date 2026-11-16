$(function(){
 const modal=$('<div>').attr({id:'telegramGroupModal',tabindex:-1}).addClass('modal fade').html('<div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5>客户专属接单群</h5><button class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><div id="telegramGroupFacts"></div><label class="form-label" for="telegramGroupTitle">群名称（仅展示）</label><input id="telegramGroupTitle" class="form-control"><label class="form-label mt-3" for="telegramGroupChat">Telegram群 chat_id</label><input id="telegramGroupChat" class="form-control" placeholder="-100…"><p class="small text-muted mt-2">先人工建立群并加入彩虹仓机器人，再绑定。一个群只能属于一个客户；群内成员下单统一归该客户。</p><div id="telegramGroupError" class="text-danger"></div></div><div class="modal-footer"><button id="telegramGroupUnbind" class="btn btn-outline-danger">解绑</button><button id="telegramGroupSave" class="btn btn-primary">确认绑定 / 更换</button></div></div></div>');$('body').append(modal);
 let userId=null,binding=null;
 modal.find('.modal-footer').prepend($('<button>').attr('id','telegramGroupProbe').addClass('btn btn-outline-secondary').text('检查机器人状态'));
 $('#telegramGroupProbe').on('click',async function(){
   if(!binding){$('#telegramGroupError').text('请先绑定接单群');return;}
   $(this).prop('disabled',true);
   try{
     const response=await fetch('/api/users/'+userId+'/telegram-group/probe',{method:'POST'});
     const result=await response.json();if(!response.ok)throw Error(result.detail||'状态检查失败');
     await load();
     $('#telegramGroupError').text(result.status==='privacy_blocked'?'Group Privacy阻止普通消息。请在BotFather关闭该设置；代码不能绕过。':result.status==='ready'?'机器人已在群内，可读取普通消息。':'无法确认群可用，请检查机器人成员状态。');
   }catch(error){$('#telegramGroupError').text(error.message);}finally{$(this).prop('disabled',false);}
 });
 async function load(){const r=await fetch('/api/users/'+userId+'/telegram-group');const rows=await r.json();if(!r.ok)throw Error(rows.detail||'读取失败');binding=rows.find(row=>row.active);$('#telegramGroupFacts').empty();const lines=binding?['绑定状态：已绑定','群名称：'+binding.title,'chat_id：'+binding.chat_id,'Bot状态：'+binding.bot_status,'最近消息：'+(binding.last_message_at?RainbowBusinessTime.format(binding.last_message_at):'尚无记录'),'最近通知：'+(binding.last_notice_at?RainbowBusinessTime.format(binding.last_notice_at):'尚无记录')]:['绑定状态：未绑定'];lines.forEach(line=>$('#telegramGroupFacts').append($('<p>').text(line)));$('#telegramGroupChat').val(binding?binding.chat_id:'');$('#telegramGroupTitle').val(binding?binding.title:'');$('#telegramGroupUnbind').prop('disabled',!binding);}
 $(document).on('click','.user-group-button',async function(){userId=Number($(this).data('user'));$('#telegramGroupError').text('');modal.modal('show');try{await load();}catch(e){$('#telegramGroupError').text(e.message);}});
 async function change(action){$('#telegramGroupError').text('');$('#telegramGroupSave,#telegramGroupUnbind').prop('disabled',true);try{const chat=action==='unbind'&&binding?binding.chat_id:$('#telegramGroupChat').val().trim();if(!/^-\d+$/.test(chat))throw Error('请填写真实Telegram群chat_id');const r=await fetch('/api/users/'+userId+'/telegram-group',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({chat_id:chat,title:$('#telegramGroupTitle').val(),action:action})});const data=await r.json();if(!r.ok)throw Error(data.detail||'保存失败');await load();}catch(e){$('#telegramGroupError').text(e.message);}finally{$('#telegramGroupSave').prop('disabled',false);$('#telegramGroupUnbind').prop('disabled',!binding);}}
 $('#telegramGroupSave').on('click',()=>change(binding?'replace':'bind'));
 $('#telegramGroupUnbind').on('click',()=>{
   if(!binding)return;
   const label=binding.title||'未命名群';
   if(!window.confirm('确认解绑接单群「'+label+'」（'+binding.chat_id+'）？\n解绑后，该群新消息不再通过此绑定归属当前客户；已有订单及历史记录不会删除。'))return;
   return change('unbind');
 });
});
