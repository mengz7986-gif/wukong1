"""Privacy-minimized counters for reviewed knowledge reuse.

No message text, account ID, address, product, or model response is stored.
"""
import fcntl
import json
import os
import tempfile
import threading
from datetime import datetime,timezone
from pathlib import Path


_WRITER_SLOT = threading.BoundedSemaphore(1)


def path():
    return Path(os.getenv('RAINBOW_LEARNING_USAGE_PATH') or Path(__file__).with_name('learning_usage.json'))


def record(*,hits,ai_called):
    kinds=sorted({str(row.get('kind')) for row in hits or [] if row.get('kind')})
    if not kinds:return
    target=path();target.parent.mkdir(parents=True,exist_ok=True)
    with open(str(target)+'.lock','a') as lock:
        os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        value=json.loads(target.read_text()) if target.exists() else {'version':2,'reviewed_hit_orders':0,'ai_calls':0,'local_without_ai':0,'kinds':{}}
        # One-time compatibility for an isolated counter created by an older
        # candidate.  The new name reports observation, not causal avoidance.
        if 'local_without_ai' not in value:
            value['local_without_ai']=int(value.pop('ai_calls_avoided',0))
        value['version']=2
        value['reviewed_hit_orders']=int(value.get('reviewed_hit_orders',0))+1
        key='ai_calls' if ai_called else 'local_without_ai';value[key]=int(value.get(key,0))+1
        for kind in kinds:value.setdefault('kinds',{})[kind]=int(value.get('kinds',{}).get(kind,0))+1
        value['updated_at']=datetime.now(timezone.utc).isoformat()
        fd,name=tempfile.mkstemp(dir=target.parent,prefix='.learning-usage-')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as output:
                json.dump(value,output,ensure_ascii=False);output.flush();os.fsync(output.fileno())
            os.chmod(name,0o600);os.replace(name,target)
        finally:
            if os.path.exists(name):os.unlink(name)
    return value


def try_record(*,hits,ai_called):
    """Best-effort, single-writer entry for a non-critical metric.

    A slow or stuck filesystem writer cannot create an unbounded queue of
    additional writers.  The caller is expected to apply its own short time
    budget and must never treat a skipped metric as an order failure.
    """
    if not _WRITER_SLOT.acquire(blocking=False):
        return None
    try:
        return record(hits=hits,ai_called=ai_called)
    finally:
        _WRITER_SLOT.release()


def summary():
    value=json.loads(path().read_text()) if path().exists() else {'version':2,'reviewed_hit_orders':0,'ai_calls':0,'local_without_ai':0,'kinds':{}}
    if 'local_without_ai' not in value:
        value['local_without_ai']=int(value.pop('ai_calls_avoided',0))
    value['version']=2
    return value
