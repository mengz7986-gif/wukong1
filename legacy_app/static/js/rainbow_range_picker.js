(function(){
  const PAIRS=[['orderStartDate','orderEndDate'],['shippingStartDate','shippingEndDate'],['operationStartFilter','operationEndFilter']];
  const pad=n=>String(n).padStart(2,'0');
  const iso=d=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
  const parse=v=>{const m=/^(\d{4})-(\d{2})-(\d{2})$/.exec(v||'');return m?new Date(+m[1],+m[2]-1,+m[3]):null};
  const add=(d,n)=>new Date(d.getFullYear(),d.getMonth(),d.getDate()+n);
  const monthAdd=(d,n)=>new Date(d.getFullYear(),d.getMonth()+n,1);
  const same=(a,b)=>a&&b&&iso(a)===iso(b);
  const between=(d,a,b)=>a&&b&&d>=a&&d<=b;
  function monthEnd(d){return new Date(d.getFullYear(),d.getMonth()+1,0)}
  function range(name){
    const now=new Date(),today=new Date(now.getFullYear(),now.getMonth(),now.getDate());
    if(name==='today')return [today,today]; if(name==='yesterday')return [add(today,-1),add(today,-1)];
    if(name==='week')return [add(today,-6),today];
    if(name==='month')return [new Date(today.getFullYear(),today.getMonth(),1),monthEnd(today)];
    const last=monthAdd(today,-1);return [last,monthEnd(last)];
  }
  function create(startId,endId){
    const start=document.getElementById(startId),end=document.getElementById(endId);
    if(!start||!end||start.dataset.rangeReady)return;
    start.dataset.rangeReady=end.dataset.rangeReady='1';
    const st=document.getElementById(startId+'Time'),et=document.getElementById(endId+'Time');
    [start,end].forEach(el=>{el.type='text';el.readOnly=true;el.inputMode='none';el.autocomplete='off';el.classList.add('rrp-date-input');});
    if(st)st.closest('.form-control')?.classList.add('rrp-time-source'); if(et)et.closest('.form-control')?.classList.add('rrp-time-source');
    const trigger=document.createElement('button');trigger.type='button';trigger.className='rrp-trigger';trigger.setAttribute('aria-haspopup','dialog');trigger.setAttribute('aria-expanded','false');
    trigger.innerHTML='<i class="far fa-calendar-alt"></i><span></span>';
    const host=start.closest('.orders-filter-field,.col,.col-md-2,.col-md-3,.col-md-4')||start.parentElement;
    host.classList.add('rrp-host'); host.appendChild(trigger);
    const panel=document.createElement('div');panel.className='rrp-panel';panel.setAttribute('role','dialog');panel.setAttribute('aria-label','选择北京时间范围');
    panel.innerHTML=`<div class="rrp-head"><strong>选择日期与时间</strong><button type="button" class="rrp-close" aria-label="关闭">×</button></div>
      <div class="rrp-values"><label>开始<input class="rrp-start-date" readonly><span class="rrp-time-field"><input class="rrp-start-time" inputmode="numeric" maxlength="8" aria-label="开始时间"><button type="button" data-clock="start" aria-label="选择开始时分秒"><i class="far fa-clock"></i></button></span></label><span>—</span><label>结束<input class="rrp-end-date" readonly><span class="rrp-time-field"><input class="rrp-end-time" inputmode="numeric" maxlength="8" aria-label="结束时间"><button type="button" data-clock="end" aria-label="选择结束时分秒"><i class="far fa-clock"></i></button></span></label></div>
      <div class="rrp-nav"><button type="button" data-nav="-1" aria-label="上个月">‹</button><strong></strong><button type="button" data-nav="1" aria-label="下个月">›</button></div>
      <div class="rrp-months"></div><div class="rrp-shortcuts"></div>
      <div class="rrp-time-pop" role="dialog" aria-label="选择时分秒"><div class="rrp-time-title"><strong></strong><button type="button" data-time-close aria-label="取消时间选择">×</button></div><div class="rrp-time-cols"></div><div class="rrp-time-actions"><button type="button" data-time-now class="btn btn-sm btn-outline-primary">此刻</button><button type="button" data-time-apply class="btn btn-sm btn-primary">确定</button></div></div>
      <div class="rrp-note">北京时间（UTC+8），24小时制；结束秒包含该秒内的亚秒记录</div>
      <div class="rrp-actions"><button type="button" data-act="clear" class="btn btn-sm btn-outline-secondary">清空</button><button type="button" data-act="cancel" class="btn btn-sm btn-light">取消</button><button type="button" data-act="apply" class="btn btn-sm btn-primary">确定</button></div>`;
    document.body.appendChild(panel);
    let draftStart=null,draftEnd=null,cursor=new Date(),choosing='start',timeTarget=null,timeDraft=[0,0,0];
    const $=q=>panel.querySelector(q), validTime=v=>/^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$/.test(v);
    function label(){trigger.querySelector('span').textContent=start.value&&end.value?`${start.value} ${st?.value||'00:00:00'} — ${end.value} ${et?.value||'23:59:59'}`:'选择日期时间范围'}
    function calendar(base){
      const box=document.createElement('section');box.className='rrp-month';
      box.innerHTML=`<h3>${base.getFullYear()}年 ${base.getMonth()+1}月</h3><div class="rrp-week"><b>一</b><b>二</b><b>三</b><b>四</b><b>五</b><b>六</b><b>日</b></div><div class="rrp-days"></div>`;
      const days=box.querySelector('.rrp-days'),first=new Date(base.getFullYear(),base.getMonth(),1),offset=(first.getDay()+6)%7,startDay=add(first,-offset);
      for(let i=0;i<42;i++){const d=add(startDay,i),b=document.createElement('button');b.type='button';b.textContent=d.getDate();b.dataset.day=iso(d);
        if(d.getMonth()!==base.getMonth())b.classList.add('muted'); if(same(d,draftStart)||same(d,draftEnd))b.classList.add('selected');if(between(d,draftStart,draftEnd))b.classList.add('in-range');
        b.onclick=()=>{if(choosing==='start'||!draftStart||draftEnd){draftStart=d;draftEnd=null;choosing='end'}else{draftEnd=d;if(draftEnd<draftStart)[draftStart,draftEnd]=[draftEnd,draftStart];choosing='start'}render()};days.appendChild(b)}return box;
    }
    function render(){
      $('.rrp-months').replaceChildren(calendar(cursor),calendar(monthAdd(cursor,1)));$('.rrp-nav strong').textContent=`${cursor.getFullYear()}年${cursor.getMonth()+1}月 — ${monthAdd(cursor,1).getFullYear()}年${monthAdd(cursor,1).getMonth()+1}月`;
      $('.rrp-start-date').value=draftStart?iso(draftStart):'';$('.rrp-end-date').value=draftEnd?iso(draftEnd):'';
    }
    function timeParts(value){const m=/^([01]\d|2[0-3]):([0-5]\d):([0-5]\d)$/.exec(value||'');return m?[+m[1],+m[2],+m[3]]:[0,0,0]}
    function beijingNow(){const p=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'}).formatToParts(new Date()).map(x=>[x.type,x.value]));return [+p.hour%24,+p.minute,+p.second]}
    function renderTime(){
      const cols=$('.rrp-time-cols');cols.replaceChildren();
      [['时',24],['分',60],['秒',60]].forEach(([name,count],column)=>{const wrap=document.createElement('div');wrap.className='rrp-time-col';wrap.setAttribute('role','listbox');wrap.setAttribute('aria-label',name);wrap.tabIndex=0;
        for(let n=0;n<count;n++){const b=document.createElement('button');b.type='button';b.textContent=pad(n);b.setAttribute('role','option');b.setAttribute('aria-selected',n===timeDraft[column]?'true':'false');if(n===timeDraft[column])b.className='selected';b.onclick=()=>{timeDraft[column]=n;renderTime()};wrap.appendChild(b)}
        wrap.onkeydown=e=>{if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();timeDraft[column]=(timeDraft[column]+(e.key==='ArrowDown'?1:-1)+count)%count;renderTime();$('.rrp-time-col:nth-child('+(column+1)+')').focus()}};cols.appendChild(wrap)});
      requestAnimationFrame(()=>cols.querySelectorAll('.selected').forEach(e=>e.scrollIntoView({block:'center'})));
    }
    function openTime(target){timeTarget=target;const input=$(target==='start'?'.rrp-start-time':'.rrp-end-time');timeDraft=timeParts(input.value);$('.rrp-time-title strong').textContent=target==='start'?'选择开始时间':'选择结束时间';renderTime();$('.rrp-time-pop').classList.add('open')}
    function closeTime(){timeTarget=null;$('.rrp-time-pop').classList.remove('open')}
    function open(){snapshot=[start.value,end.value,st?.value,et?.value];draftStart=parse(start.value);draftEnd=parse(end.value);cursor=new Date((draftStart||new Date()).getFullYear(),(draftStart||new Date()).getMonth(),1);choosing=draftStart&&!draftEnd?'end':'start';$('.rrp-start-time').value=st?.value||'00:00:00';$('.rrp-end-time').value=et?.value||'23:59:59';render();panel.classList.add('open');trigger.setAttribute('aria-expanded','true');position()}
    function close(){closeTime();panel.classList.remove('open');trigger.setAttribute('aria-expanded','false')}
    function position(){const mobile=innerWidth<768;if(mobile){panel.style.cssText=''}else{const r=trigger.getBoundingClientRect();panel.style.left=Math.max(12,Math.min(innerWidth-panel.offsetWidth-12,r.left))+'px';panel.style.top=(r.bottom+8+scrollY)+'px'}}
    trigger.onclick=open;start.onclick=open;end.onclick=open;$('.rrp-close').onclick=close;$('[data-act="cancel"]').onclick=close;
    panel.querySelectorAll('[data-clock]').forEach(b=>b.onclick=()=>openTime(b.dataset.clock));$('[data-time-close]').onclick=closeTime;
    $('[data-time-now]').onclick=()=>{timeDraft=beijingNow();renderTime()};
    $('[data-time-apply]').onclick=()=>{if(!timeTarget)return;$(timeTarget==='start'?'.rrp-start-time':'.rrp-end-time').value=timeDraft.map(pad).join(':');closeTime()};
    panel.querySelectorAll('[data-nav]').forEach(b=>b.onclick=()=>{cursor=monthAdd(cursor,+b.dataset.nav);render()});
    [['今天','today'],['昨天','yesterday'],['近7天','week'],['本月','month'],['上月','last']].forEach(([t,k])=>{const b=document.createElement('button');b.type='button';b.className='btn btn-sm btn-outline-primary';b.textContent=t;b.onclick=()=>{[draftStart,draftEnd]=range(k);cursor=new Date(draftStart.getFullYear(),draftStart.getMonth(),1);render()};$('.rrp-shortcuts').appendChild(b)});
    $('[data-act="clear"]').onclick=()=>{start.value=end.value='';if(st)st.value='00:00:00';if(et)et.value='23:59:59';label();close();start.dispatchEvent(new Event('change',{bubbles:true}))};
    $('[data-act="apply"]').onclick=()=>{const sv=$('.rrp-start-time').value,ev=$('.rrp-end-time').value;if(!draftStart||!draftEnd)return alert('请选择开始与结束日期');if(!validTime(sv)||!validTime(ev))return alert('请输入24小时制 HH:mm:ss');start.value=iso(draftStart);end.value=iso(draftEnd);if(st)st.value=sv;if(et)et.value=ev;label();close();start.dispatchEvent(new Event('change',{bubbles:true}));end.dispatchEvent(new Event('change',{bubbles:true}))};
    document.addEventListener('pointerdown',e=>{if(panel.classList.contains('open')&&!panel.contains(e.target)&&!host.contains(e.target))close()});
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('.rrp-time-pop').classList.contains('open'))closeTime();else if(e.key==='Escape'&&panel.classList.contains('open')){close();trigger.focus()}});
    window.addEventListener('resize',()=>panel.classList.contains('open')&&position());label();
  }
  function mount(){PAIRS.forEach(p=>create(...p));document.querySelectorAll('[data-range-start][data-range-end]').forEach(el=>create(el.dataset.rangeStart,el.dataset.rangeEnd))}
  window.RainbowRangePicker={mount,create};document.readyState==='loading'?document.addEventListener('DOMContentLoaded',()=>setTimeout(mount)):setTimeout(mount);
})();
