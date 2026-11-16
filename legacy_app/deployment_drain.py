"""Durable admission pause and OS-owned activity locks, no business mutation.

Pause/claim linearize on admission.lock. Activity locks span a whole worker
or notification operation and disappear on process death; persisted leases
and outbox truth must ALSO be checked before stopping services.
"""
import fcntl,json,os,re,tempfile,time,math
from pathlib import Path
from contextlib import contextmanager

def path():return Path(os.getenv('RAINBOW_DEPLOY_DRAIN_PATH') or Path(__file__).with_name('.deployment-drain.json'))

def _open(suffix):
    p=Path(str(path())+suffix);p.parent.mkdir(parents=True,exist_ok=True)
    return os.fdopen(os.open(str(p),os.O_CREAT|os.O_RDWR,0o600),'a+')

def _read():
    p=path()
    if not p.exists():return dict(version=1,paused=False,generation=0,intervals=[])
    try:
        s=json.loads(p.read_text())
        if s.get('version')!=1 or type(s.get('paused')) is not bool or type(s.get('generation')) is not int or not isinstance(s.get('intervals'),list):raise ValueError()
        if type(s.get('submission_hold',False)) is not bool:raise ValueError()
        if s.get('submission_hold') and not s['paused']:raise ValueError()
        last=0
        for lo,hi in s['intervals']:
            if type(lo) not in (int,float) or type(hi) not in (int,float) or not math.isfinite(lo) or not math.isfinite(hi) or not last<=lo<=hi:raise ValueError()
            last=hi
        if s['paused'] and (not isinstance(s.get('paused_at'),(int,float)) or not re.fullmatch('[a-f0-9]{32}',s.get('owner',''))):raise ValueError()
        return s
    except Exception:raise RuntimeError('DEPLOYMENT_DRAIN_STATE_INVALID') from None

def _write(state):
    p=path();fd,tmp=tempfile.mkstemp(dir=p.parent,prefix='.drain-')
    try:
        with os.fdopen(fd,'w') as f:json.dump(state,f);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o600);os.replace(tmp,p)
        fd=os.open(str(p.parent),os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def snapshot():
    with _open('.admission.lock') as f:
        fcntl.flock(f,fcntl.LOCK_SH);return _read()

def pause(owner,now=None):
    if not isinstance(owner,str) or not re.fullmatch('[a-f0-9]{32}',owner):raise ValueError('DRAIN_OWNER_INVALID')
    with _open('.admission.lock') as f:
        fcntl.flock(f,fcntl.LOCK_EX);s=_read()
        if s['paused']:
            if s['owner']!=owner:raise RuntimeError('DRAIN_OWNED_BY_OTHER_OPERATION')
            return s
        if len(s['intervals'])>=1024:raise RuntimeError('DRAIN_HISTORY_REQUIRES_REVIEW')
        start=time.time() if now is None else now
        if type(start) not in (int,float) or not math.isfinite(start) or start<0 or (s['intervals'] and start<s['intervals'][-1][1]):raise RuntimeError('DRAIN_CLOCK_ROLLBACK')
        s.update(paused=True,owner=owner,paused_at=start,generation=s['generation']+1)
        _write(s);return s

def resume(owner,generation,now=None):
    with _open('.admission.lock') as f:
        fcntl.flock(f,fcntl.LOCK_EX);s=_read()
        if not s['paused'] or s.get('owner')!=owner or s['generation']!=generation:raise RuntimeError('DRAIN_RESUME_FENCE_MISMATCH')
        if s.get('submission_hold'):raise RuntimeError('DRAIN_PRICE_COMPATIBILITY_HOLD_REQUIRES_VERIFICATION')
        end=time.time() if now is None else now
        if type(end) not in (int,float) or not math.isfinite(end) or end<s['paused_at']:raise RuntimeError('DRAIN_CLOCK_ROLLBACK')
        s['intervals'].append([s['paused_at'],end]);s.update(paused=False,owner=None,paused_at=None)
        _write(s);return s

def hold_submissions(owner,generation):
    """Latch a manual-recovery hold; ordinary resume must never clear it."""
    with _open('.admission.lock') as f:
        fcntl.flock(f,fcntl.LOCK_EX);s=_read()
        if not s['paused'] or s.get('owner')!=owner or s['generation']!=generation:
            raise RuntimeError('DRAIN_SUBMISSION_HOLD_FENCE_MISMATCH')
        s['submission_hold']=True
        s['submission_hold_reason']='ROLLBACK_PRICE_SEMANTICS_UNVERIFIED'
        _write(s);return s

def begin_activity():
    with _open('.admission.lock') as f:
        # Called both on the event loop and in executor threads. A controller
        # holding EX must make workers yield, never prevent process shutdown.
        try:fcntl.flock(f,fcntl.LOCK_SH|fcntl.LOCK_NB)
        except BlockingIOError:return None
        if _read()['paused']:return None
        active=_open('.activity.lock')
        try:fcntl.flock(active,fcntl.LOCK_SH|fcntl.LOCK_NB)
        except BlockingIOError:active.close();return None
        return active

def end_activity(handle):
    if handle is not None:handle.close()

def release_job(job):
    if job:end_activity(job.pop('_deployment_activity',None))

def effective_wait_age(received,now=None,state=None):
    now=time.time() if now is None else now;s=snapshot() if state is None else state
    intervals=list(s['intervals'])
    if s['paused']:intervals.append([s['paused_at'],now])
    credit=sum(max(0,min(now,hi)-max(received,lo)) for lo,hi in intervals)
    return max(0,now-received-credit)

@contextmanager
def idle_barrier(database,owner,generation,history_policy=None):
    """Reserved SQLite writer lock closes the OLD-worker bootstrap race.

    No SQL writes. Release this context before stopping cooperative services;
    the durable owner/generation pause remains their admission fence.
    NEVER wait here for an active job to finish (it needs SQL).
    Any active/expired task or ambiguous sending receipt aborts immediately.
    """
    with _open('.admission.lock') as admission:
        fcntl.flock(admission,fcntl.LOCK_EX);s=_read()
        if not s['paused'] or s.get('owner')!=owner or s['generation']!=generation:raise RuntimeError('DRAIN_BARRIER_OWNER_MISMATCH')
        with _open('.activity.lock') as activity:
            try:fcntl.flock(activity,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('DRAIN_ACTIVE_OPERATIONS') from None
            with database.SessionLocal() as session:
                session.connection().exec_driver_sql('BEGIN IMMEDIATE')
                from sqlalchemy import text,inspect
                processing=session.execute(text("select count(*) from telegram_order_jobs where state='PROCESSING'")).scalar()
                if processing:raise RuntimeError('DRAIN_PROCESSING_OR_CRASHED_LEASE')
                from deployment_history_gate import validate
                historical=validate(session,history_policy)
                from success_receipt_audit import assert_quiet
                assert_quiet()
                try:yield dict(paused=True,generation=generation,processing=0,historical_audit_exceptions=historical)
                finally:session.rollback()
