// Shared second-precision controls. Dates remain native calendar inputs.
window.RainbowTimeRange={
  values:function(startId,endId,required){
    function value(id,end){
      const date=$('#'+id).val(),time=$('#'+id+'Time').val()||(end?'23:59:59':'00:00:00');
      if(!date)return '';
      if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||!/^\d{2}:\d{2}(?::\d{2})?$/.test(time))throw Error('请输入有效日期和时分秒');
      const full=date+'T'+(time.length===5?time+':00':time);
      const numbers=time.split(':').map(Number);
      if(numbers[0]>23||numbers[1]>59||(numbers[2]||0)>59)throw Error('北京时间请输入24小时制 HH:mm:ss，不使用AM/PM');
      const stamp=Date.parse(full+'+08:00');
      if(!Number.isFinite(stamp)||new Date(stamp+8*3600000).toISOString().slice(0,19)!==full)throw Error('日期时间无效');
      return full;
    }
    const start=value(startId,false),end=value(endId,true);
    if(required&&(!start||!end))throw Error('请选择开始与结束日期时间');
    if(start&&end&&start>end)throw Error('开始时间不能晚于结束时间');
    return {start,end};
  },
  mount:function(startId,endId){
    [startId,endId].forEach((id,index)=>{
      const date=$('#'+id);
      if(!date.length||document.getElementById(id+'Time'))return;
      date.after($('<input>').attr({id:id+'Time',type:'text',inputmode:'numeric',maxlength:'8',placeholder:'HH:mm:ss',pattern:'([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]','aria-label':index?'结束时分秒（北京时间24小时制）':'开始时分秒（北京时间24小时制）'}).addClass('form-control').val(index?'23:59:59':'00:00:00'));
    });
    const first=$('#'+startId);
    if(first.length&&!first.parent().find('.beijing-range-note').length){
      first.parent().append($('<small>').addClass('text-muted beijing-range-note').text('北京时间（Asia/Shanghai，UTC+8），24小时制；结束秒包含亚秒记录'));
      ['今天','昨天'].forEach((label,offset)=>first.parent().append($('<button>').attr({type:'button'}).addClass('btn btn-sm btn-outline-secondary ms-1').text(label).on('click',()=>{
        const day=RainbowBusinessTime.today(-offset);$('#'+startId+',#'+endId).val(day);
        $('#'+startId+'Time').val('00:00:00');$('#'+endId+'Time').val('23:59:59');
      })));
    }
  }
};
$(function(){[['orderStartDate','orderEndDate'],['shippingStartDate','shippingEndDate'],['operationStartFilter','operationEndFilter']].forEach(pair=>RainbowTimeRange.mount(...pair));});
