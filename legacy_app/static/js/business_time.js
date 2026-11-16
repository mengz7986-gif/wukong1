// Storage is UTC (including naive ORM ISO values). Inputs/display are Beijing time.
window.RainbowBusinessTime=(function(){
  const zone='Asia/Shanghai';
  function parse(value){
    if(value==null||value==='')return null;
    if(value instanceof Date)return Number.isFinite(value.getTime())?value:null;
    if(typeof value==='number'){const date=new Date(value);return Number.isFinite(date.getTime())?date:null;}
    let text=String(value).trim().replace(' ','T');
    if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$/i.test(text))return null;
    if(!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text))text+='Z';
    const date=new Date(text);return Number.isFinite(date.getTime())?date:null;
  }
  function parts(value){
    const date=parse(value);if(!date)return null;
    const options={timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'};
    return Object.fromEntries(new Intl.DateTimeFormat('en-GB',options).formatToParts(date).map(p=>[p.type,p.value]));
  }
  function date(value){const p=parts(value);return p?`${p.year}-${p.month}-${p.day}`:'-';}
  function format(value){const p=parts(value);return p?`${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`:'-';}
  function today(offsetDays=0,now=Date.now()){
    const day=date(now);if(day==='-')return day;
    return new Date(Date.parse(day+'T00:00:00Z')+Number(offsetDays)*86400000).toISOString().slice(0,10);
  }
  return {zone,parse,format,date,today};
})();
