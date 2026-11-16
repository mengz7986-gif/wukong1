async function showProxyPermissions(){
    try{
        const response=await fetch('/api/proxy-confirmation/grants',{cache:'no-store'});
        const data=await response.json();if(!response.ok)throw Error(data.detail||'读取权限失败');
        let box=document.getElementById('proxyPermissions');
        if(!box){box=document.createElement('dialog');box.id='proxyPermissions';box.style.cssText='width:min(92vw,620px);max-height:85dvh;overflow:auto;border:1px solid #ddd;border-radius:16px;padding:20px';document.body.append(box);}
        box.replaceChildren();const title=document.createElement('h3');title.textContent='管理员代确认权限';box.append(title);
        const note=document.createElement('p');note.textContent='仅允许已核实的5个用户ID，在有权限的接单群代确认。客户账单归属不变。';box.append(note);
        for(const grant of data.grants){
            const row=document.createElement('div');row.style.cssText='display:flex;gap:12px;align-items:center;justify-content:space-between;padding:12px 0;flex-wrap:wrap';
            const label=document.createElement('span');label.textContent='@'+grant.username+' · '+grant.telegram_id;
            const button=document.createElement('button');button.type='button';button.className='btn btn-sm '+(grant.enabled?'btn-outline-danger':'btn-outline-primary');
            button.textContent=grant.identity_verified?(grant.enabled?'撤销权限':'启用权限'):'身份待核实';button.disabled=!grant.identity_verified;
            button.onclick=async()=>{
                if(!confirm((grant.enabled?'撤销':'启用')+' @'+grant.username+' 的代确认权限？'))return;
                button.disabled=true;
                try{const r=await fetch('/api/proxy-confirmation/grants',{method:'POST',headers:{'Content-Type':'application/json','X-Proxy-CSRF':data.csrf},body:JSON.stringify({telegram_id:grant.telegram_id,enabled:!grant.enabled})});
                    const value=await r.json();if(!r.ok)throw Error(value.detail||'保存失败');await showProxyPermissions();
                }catch(error){button.disabled=false;alert(error.message);}
            };
            row.append(label,button);box.append(row);
        }
        const close=document.createElement('button');close.className='btn btn-secondary';close.textContent='关闭';close.onclick=()=>box.close();box.append(close);
        if(!box.open)box.showModal();
    }catch(error){alert(error.message||'权限读取失败');}
}
