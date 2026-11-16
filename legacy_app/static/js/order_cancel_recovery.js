/* Frozen cancellation recovery. Transport is injected; no automatic resume. */
(function(root) {
    'use strict';
    function create(options) {
        var task=null,busy=false,key='rainbow.cancel-recovery.v1:'+options.owner;
        function validate(value) {
            if(!value || value.version!==1 || value.owner!==options.owner || !Array.isArray(value.ids) ||
                !value.ids.length || value.ids.length>5000 || new Set(value.ids).size!==value.ids.length ||
                value.ids.some(id=>typeof id!=='string' || !/^[1-9]\d*$/.test(id)) ||
                typeof value.reason!=='string' || !value.reason.trim() || Array.from(value.reason).length>2000)
                throw new Error('保留的取消批次无效，请联系管理员核对，未执行取消');
            return value;
        }
        if(options.storage) {
            var saved=options.storage.getItem(key);
            if(saved) task=validate(JSON.parse(saved));
        }
        function current(){return task?JSON.parse(JSON.stringify(task)):null;}
        function start(ids,reason) {
            if(task || busy)throw new Error('已有未完成取消批次，请先核对原批次');
            var value=validate({version:1,owner:options.owner,ids:Array.from(new Set(ids.map(String))),reason:reason.trim()});
            if(!options.storage)throw new Error('浏览器无法保留原组选单，未开始取消，请检查浏览器存储权限');
            options.storage.setItem(key,JSON.stringify(value));
            task=value;return current();
        }
        async function inspectTask() {
            var rows=[];
            for(var offset=0;offset<task.ids.length;offset+=100) {
                var ids=task.ids.slice(offset,offset+100),response=await options.check(ids);
                var group=response&&response.orders;
                if(!Array.isArray(group) || group.length!==ids.length ||
                    new Set(group.map(row=>String(row.order_id))).size!==ids.length ||
                    group.some(row=>!ids.includes(String(row.order_id)) || typeof row.status!=='string'))
                    throw new Error('核对结果不完整，已停止；原组选单仍保留');
                rows=rows.concat(group);
            }
            return rows;
        }
        async function run() {
            if(busy)throw new Error('正在核对或取消，请勿重复点击');
            if(!task)throw new Error('没有待恢复的取消批次');
            busy=true;
            try {
                var rows=await inspectTask();
                // Finish checking the entire frozen selection before any write.
                var pending=rows.filter(row=>['pending','split_pending','confirmed','shipped'].includes(row.status));
                for(var offset=0;offset<pending.length;offset+=100) {
                    var ids=pending.slice(offset,offset+100).map(row=>String(row.order_id));
                    var result=await options.submit(ids,task.reason),items=result&&result.results;
                    if(!Array.isArray(items) || items.length!==ids.length ||
                        new Set(items.map(row=>String(row.order_id))).size!==ids.length ||
                        items.some(row=>!ids.includes(String(row.order_id)) || typeof row.success!=='boolean'))
                        throw new Error('取消响应不完整，结果待核对；原组选单仍保留');
                }
                // A response saying success is not the final authority: re-read.
                rows=await inspectTask();
                var cancelled=rows.filter(row=>row.status==='cancelled').length;
                var result={total:task.ids.length,cancelled:cancelled,
                    remaining:rows.filter(row=>row.status!=='cancelled'),done:cancelled===task.ids.length};
                if(result.done) {
                    options.storage.removeItem(key);
                    task=null;
                }
                return result;
            } finally {busy=false;}
        }
        return {current:current,start:start,run:run,isBusy:()=>busy};
    }
    root.RainbowCancelRecovery={create:create};
})(typeof window==='undefined'?globalThis:window);
