(() => {
  'use strict';
  async function load(){const r=await fetch('/api/settings/bundles');if(!r.ok)return;const data=await r.json();const box=document.getElementById('bundleShippingRule');box.hidden=false;document.getElementById('bundleShippingMax').value=data.rule.max_portions;
    document.getElementById('saveBundleShippingRule').onclick=async()=>{const error=document.getElementById('bundleShippingError');try{const r=await fetch('/api/settings/bundles',{method:'POST',headers:{'Content-Type':'application/json','X-Bundle-CSRF':data.csrf},body:JSON.stringify({max_portions:Number(document.getElementById('bundleShippingMax').value)})});const body=await r.json();if(!r.ok)throw new Error(typeof body.detail==='string'?body.detail:'保存失败');error.textContent='';}catch(e){error.textContent=e.message;}};
  }
  load().catch(()=>{});
})();
