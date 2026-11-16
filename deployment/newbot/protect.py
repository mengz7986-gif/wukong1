"""Standalone new-server protection. Not the historical project backup script.

Only explicit SQLite sources; source directory trees are NEVER archived.
"""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import urllib.parse
import urllib.error
import uuid

DB=Path(os.getenv('NEWBOT_PROTECTION_DB','/var/lib/newbot/db/orders.sqlite3'))
BACKUPS=Path('/var/backups/newbot')
STATE=Path('/var/lib/newbot/protection')
MIB=1024**2
GIB=1024**3

def digest(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(MIB),b''):h.update(b)
    return h.hexdigest()

def health(connection):
    if connection.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise RuntimeError('INTEGRITY_FAILED')
    if connection.execute('PRAGMA foreign_key_check').fetchall():raise RuntimeError('FOREIGN_KEY_FAILED')

def snapshot(source,target):
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(target)) as dst:
        deadline=time.monotonic()+300
        def progress(status,remaining,total):
            if time.monotonic()>deadline:raise RuntimeError('SNAPSHOT_DEADLINE')
            if usage(target.parent)['block_large_backup']:raise RuntimeError('SNAPSHOT_DISK_GUARD')
        src.backup(dst,pages=256,sleep=0.05,progress=progress)
        health(dst)

def usage(root):
    st=os.statvfs(root)
    total=st.f_blocks*st.f_frsize
    free=st.f_bavail*st.f_frsize
    used=100*(1-free/total)
    inode=100*(1-st.f_favail/max(st.f_files,1))
    severity='critical' if used>=95 or free<2*GIB or inode>=95 else 'urgent' if used>=90 or free<5*GIB or inode>=90 else 'warning' if used>=80 or free<10*GIB or inode>=80 else 'normal'
    return dict(used_percent=round(used,2),free_bytes=free,inode_percent=round(inode,2),severity=severity,
                pause_optional_archives=severity in ('urgent','critical'),block_large_backup=severity=='critical')

def atomic_json(p,data):
    fd,name=tempfile.mkstemp(dir=p.parent,prefix='.state-')
    try:
        with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(name,p)
    finally:
        if os.path.exists(name):os.unlink(name)

def notify(kind,body):
    config_path=Path('/etc/newbot/alerts.json')
    if not config_path.exists():return dict(configured=False)
    config=json.loads(config_path.read_text())
    recipients=config.get('telegram_ids',[])
    if len(recipients)!=5 or len(set(recipients))!=5 or any(not str(x).isdigit() for x in recipients):raise RuntimeError('ALERT_RECIPIENTS_INVALID')
    env={}
    for line in (DB.parent/'runtime.env').read_text().splitlines():
        k,sep,v=line.partition('=')
        if sep:env[k]=json.loads(v)
    token=env['BOT_TOKEN']
    p=STATE/'telegram-alerts.json'
    state=json.loads(p.read_text()) if p.exists() else {}
    now=time.time();results={}
    for chat in recipients:
        key=str(chat)+'/'+kind
        previous=state.get(key,{})
        if now-previous.get('at',0)<4*3600:
            results[str(chat)]='cooldown';continue
        # Persist intent BEFORE sending. Uncertain transport is not retried.
        state[key]=dict(at=now,status='SENDING')
        atomic_json(p,state)
        data=urllib.parse.urlencode({'chat_id':chat,'text':body}).encode()
        try:
            request=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',data=data)
            with urllib.request.urlopen(request,timeout=5) as response:result=json.load(response)
            status='SENT' if result.get('ok') else 'REJECTED'
            detail='' if result.get('ok') else str(result.get('description',''))[:160]
        except urllib.error.HTTPError as e:
            status='REJECTED'
            try:detail=json.loads(e.read()).get('description','')[:160]
            except Exception:detail='HTTP_'+str(e.code)
        except Exception:
            status='UNKNOWN';detail='Transport uncertain; no automatic resend'
        state[key]=dict(at=now,status=status,detail=detail)
        atomic_json(p,state);results[str(chat)]=status
    return results

def verify(directory):
    manifest=json.loads((directory/'manifest.json').read_text())
    if manifest.get('kind')!='newbot-online-backup-v1':raise RuntimeError('UNKNOWN_BACKUP')
    names=set(manifest['files'])
    if 'orders.sqlite3' not in names or any(Path(n).name!=n for n in names):raise RuntimeError('INVALID_MANIFEST')
    for name,fact in manifest['files'].items():
        p=directory/name
        if p.is_symlink() or digest(p)!=fact['sha256']:raise RuntimeError('HASH_MISMATCH')
        if fact['sqlite']:
            with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as c:health(c)
    return manifest

def restore_proof(directory):
    verify(directory)
    with tempfile.TemporaryDirectory(prefix='.restore-',dir=BACKUPS) as td:
        restored=Path(td)/'restored'
        shutil.copytree(directory,restored)
        verify(restored)

def backup(reason):
    if not DB.is_file():raise RuntimeError('BUSINESS_DATABASE_MISSING')
    if DB.is_symlink() or BACKUPS.is_symlink() or DB.parent==BACKUPS or BACKUPS in DB.parents:raise RuntimeError('UNSAFE_BACKUP_PATH')
    disk=usage(BACKUPS)
    size=sum(p.stat().st_size for p in DB.parent.glob('*.sqlite3') if p.is_file())
    retained=sum(p.stat().st_size for p in BACKUPS.rglob('*') if p.is_file())
    if disk['pause_optional_archives'] or disk['free_bytes']<max(10*GIB,size*4) or retained+size*2>5*GIB:raise RuntimeError('BACKUP_CAPACITY_GUARD')
    staging=Path(tempfile.mkdtemp(prefix='.snapshot-',dir=BACKUPS))
    try:
        manifest=dict(kind='newbot-online-backup-v1',reason=reason,created_at=datetime.now(timezone.utc).isoformat(),files={})
        # Fixed file list prevents recursive backup and bounds snapshot contents.
        sources=[DB,DB.parent/'verified-addresses.sqlite3',DB.parent/'recognition-reviews.sqlite3',DB.parent/'cloud-address-trust.sqlite3']
        for source in sources:
            if not source.exists():continue
            if source.is_symlink() or not source.is_file():raise RuntimeError('UNSAFE_SOURCE')
            output=staging/source.name
            snapshot(source,output)
            manifest['files'][output.name]=dict(sha256=digest(output),sqlite=True)
        config=DB.parent
        for source in [config/'runtime.env',config/'local-settings.key',config/'ai_settings.json',config/'aliyun_address_settings.json',config/'recipient-policy.json',config/'imported-rule-dictionary-20261021.json']:
            if not source.exists():raise RuntimeError('MISSING_BACKUP_CONFIG')
            output=staging/source.name
            if source.is_symlink():raise RuntimeError('UNSAFE_CONFIG_SOURCE')
            shutil.copyfile(source,output)
            manifest['files'][output.name]=dict(sha256=digest(output),sqlite=False)
        app=Path('/opt/newbot/current/legacy_app')
        policy=Path('/etc/newbot/ingestion-policy.json')
        if policy.exists():
            if policy.is_symlink():raise RuntimeError('UNSAFE_INGESTION_POLICY')
            output=staging/'ingestion-policy.json';before=digest(policy);shutil.copyfile(policy,output)
            if digest(output)!=before or digest(policy)!=before:raise RuntimeError('POLICY_CHANGED_DURING_BACKUP')
            manifest['files'][output.name]=dict(sha256=before,sqlite=False)
        for relative in ['settings.json','learning_evidence.json','learning_evidence_access.json','ai_usage_ledger.json','aliyun_cost_ledger.json','aliyun_address_settings.usage.json','data/inventory_image_warehouse_1.json','data/inventory_image_warehouse_2.json']:
            source=app/relative
            if not source.exists():continue
            if source.is_symlink():raise RuntimeError('UNSAFE_CONFIG_SOURCE')
            output=staging/('app-'+relative.replace('/','--'))
            # Atomic producer saves plus before/after check avoid mixed config.
            before=digest(source);shutil.copyfile(source,output)
            if digest(output)!=before or digest(source)!=before:raise RuntimeError('CONFIG_CHANGED_DURING_BACKUP')
            manifest['files'][output.name]=dict(sha256=digest(output),sqlite=False)
        atomic_json(staging/'manifest.json',manifest)
        restore_proof(staging)
        dest=BACKUPS/('snapshot-'+uuid.uuid4().hex)
        staging.rename(dest)
        atomic_json(STATE/'last-backup.json',dict(path=str(dest),verified=True,created_at=manifest['created_at']))
        retention(dest)
        return dict(backup=str(dest),restore_verified=True)
    finally:
        if staging.exists():shutil.rmtree(staging)

def retention(latest):
    # Deployment/manual snapshots are NEVER automatic deletion candidates.
    records=[]
    for p in BACKUPS.glob('snapshot-*'):
        if p.is_symlink() or p.parent!=BACKUPS:continue
        try:
            m=verify(p)
            if m['reason']=='routine':records.append((datetime.fromisoformat(m['created_at']),p))
        except Exception:continue
    records.sort(reverse=True)
    keep={latest}; days=set();weeks=set();months=set()
    for dt,p in records:
        day=dt.date().isoformat();week=dt.strftime('%G-%V');month=dt.strftime('%Y-%m')
        if day not in days and len(days)<7:keep.add(p);days.add(day)
        if week not in weeks and len(weeks)<4:keep.add(p);weeks.add(week)
        if month not in months and len(months)<3:keep.add(p);months.add(month)
    restore_proof(latest)
    candidates=[str(p) for _,p in records if p not in keep]
    atomic_json(STATE/'retention-candidates.json',dict(policy='7 daily / 4 weekly / 3 monthly',candidates=candidates,
        deletion_enabled=os.getenv('NEWBOT_ALLOW_ROUTINE_RETENTION')=='1'))
    # During migration all old recoverable copies must remain untouched.
    if os.getenv('NEWBOT_ALLOW_ROUTINE_RETENTION')!='1':return
    for _,p in records:
        if p not in keep:
            # Scope is only our valid routine snapshot and never sole recovery.
            verify(latest)
            shutil.rmtree(p)

def monitor():
    report=usage('/var/lib/newbot')
    report['at']=datetime.now(timezone.utc).isoformat()
    paths=[DB,Path(str(DB)+'-wal'),Path(str(DB)+'-shm')]
    report['files']={p.name:p.stat().st_size if p.exists() else 0 for p in paths}
    report['directory_bytes']={}
    for directory in ['/var/log/newbot','/var/log/nginx','/var/log/journal','/var/cache/newbot','/var/backups/newbot','/var/backups/newbot-deployment']:
        report['directory_bytes'][directory]=sum(p.stat().st_size for p in Path(directory).rglob('*') if p.is_file() and not p.is_symlink())
    with closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True,timeout=5)) as c:
        report['table_rows']={t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['orders','telegram_order_jobs','inventory_batches','stock_movements','transactions','payments']}
    previous=STATE/'status.json'
    if previous.exists():
        old=json.loads(previous.read_text()); report['db_growth_bytes']=report['files'][DB.name]-old.get('files',{}).get(DB.name,0)
    atomic_json(previous,report)
    # Daily growth is measured from the first sample, not the preceding minute.
    today=STATE/('daily-'+datetime.now(timezone.utc).strftime('%Y-%m-%d')+'.json')
    baseline=json.loads(today.read_text()).get('baseline',{}) if today.exists() else {}
    if not baseline:baseline=dict(files=report['files'],directory_bytes=report['directory_bytes'],table_rows=report['table_rows'])
    report['baseline']=baseline
    report['daily_db_growth_bytes']=report['files'][DB.name]-baseline['files'].get(DB.name,0)
    report['daily_table_growth']={t:n-baseline['table_rows'].get(t,0) for t,n in report['table_rows'].items()}
    atomic_json(today,report)
    atomic_json(previous,report)
    daily=sorted(STATE.glob('daily-????-??-??.json'))
    for p in daily[:-31]:p.unlink()
    if report['severity']!='normal':
        subprocess.run(['logger','-p','daemon.err','-t','newbot-disk',json.dumps(report)],check=False)
        report['notification_results']=notify('disk-'+report['severity'],
            '【新机器人磁盘告警】'+report['severity']+'\n服务器：VM-H4YxjkvwZm / 103.228.64.72\n磁盘使用率：'+str(report['used_percent'])+'%\n剩余：'+str(round(report['free_bytes']/GIB,2))+'GB\ninode使用率：'+str(report['inode_percent'])+'%\n不会自动删除订单、库存或账务，请检查。')
    # Only disposable generated previews; never business records/config.
    cache=Path('/var/cache/newbot')
    files=sorted((p for p in cache.glob('inventory-preview-*') if p.is_file() and not p.is_symlink()),key=lambda p:p.stat().st_mtime)
    total=sum(p.stat().st_size for p in files)
    for p in files:
        if total<=100*MIB:break
        total-=p.stat().st_size;p.unlink()
    return report

def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['backup','monitor','selftest','alert-test']);parser.add_argument('--reason',default='routine',choices=['routine','deployment'])
    args=parser.parse_args();os.umask(0o077)
    STATE.mkdir(parents=True,exist_ok=True);BACKUPS.mkdir(parents=True,exist_ok=True)
    with (STATE/'protection.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            if args.command=='backup':result=backup(args.reason)
            elif args.command=='monitor':result=monitor()
            elif args.command=='alert-test':result=notify('delivery-test','【新机器人容量告警测试】这是一条通知通道验证消息，不代表磁盘异常。服务器：VM-H4YxjkvwZm。未创建订单，接单机器人未启动。')
            else:
                # Synthetic threshold inputs, no disk filling or orders.
                from unittest.mock import patch
                outcomes=[]
                for used in [80,90,95]:
                    fake=type('Stat',(),dict(f_blocks=1000,f_frsize=GIB,f_bavail=1000-used*10,f_files=1000,f_favail=999))()
                    with patch('os.statvfs',return_value=fake):outcomes.append(usage('/')['severity'])
                assert outcomes==['warning','urgent','critical']
                result=dict(threshold_tests=outcomes,passed=True)
            print(json.dumps(result,ensure_ascii=False))
        except Exception as e:
            atomic_json(STATE/'last-error.json',dict(at=datetime.now(timezone.utc).isoformat(),error=type(e).__name__,operation=args.command))
            subprocess.run(['logger','-p','daemon.err','-t','newbot-protection','operation failed: '+args.command],check=False)
            try:notify('protection-failure-'+args.command,'【新机器人保护任务异常】'+args.command+' 执行失败。服务器：VM-H4YxjkvwZm；原有有效备份保留，请检查。')
            except Exception:pass
            raise

if __name__=='__main__':main()
