/* Units change display only. The server validates role, CSRF and allowed values. */
async function editProductUnit(productId){
    try{
        const response=await fetch('/api/products/'+Number(productId)+'/unit',{cache:'no-store'});
        const value=await response.json();if(!response.ok)throw new Error(value.detail||'读取失败');
        const next=prompt('计量单位：'+value.allowed.join('、')+'；输入“继承”可清除商品覆盖。',value.unit);
        if(next===null)return;
        const unit=next.trim()==='继承'?null:next.trim();
        if(unit!==null&&!value.allowed.includes(unit))throw new Error('请选择列出的计量单位');
        const saved=await fetch('/api/products/'+Number(productId)+'/unit',{
            method:'POST',headers:{'Content-Type':'application/json','X-Unit-CSRF':value.csrf},body:JSON.stringify({unit})});
        const result=await saved.json();if(!saved.ok)throw new Error(result.detail||'保存失败');
        alert('已保存计量单位：'+result.unit+'。库存数量和历史订单不变。');
        if(typeof loadProducts==='function')loadProducts();
    }catch(error){alert(error.message||'单位设置失败');}
}
