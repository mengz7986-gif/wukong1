"""Run on the fresh server only; stages candidate without switching business DB."""
import hashlib,json,os,pwd,shutil,sqlite3,subprocess,sys
from pathlib import Path
from data_evidence import evidence

release=Path(sys.argv[1]).resolve()
assert release.parent==Path('/opt/newbot/releases') and release.name.startswith('release-')
live=Path('/var/lib/newbot/db/orders.sqlite3')
assert not live.exists(), 'Server business DB exists: stop and compare before proceeding'
for root in [Path('/srv'),Path('/opt'),Path('/var/lib/newbot')]:
    if root==Path('/opt'):continue  # release snapshot is known staging, not live
    for p in root.rglob('*.db'):
        raise RuntimeError('Unexpected server database: review required')
manifest=json.loads((release/'manifest.json').read_text())
for name,digest in manifest.items():
    p=release/name
    assert not p.is_symlink() and p.is_file()
    assert hashlib.sha256(p.read_bytes()).hexdigest()==digest
candidate=Path('/var/lib/newbot/candidates')/release.name
assert not candidate.exists(), 'Never overwrite existing candidate'
candidate.parent.mkdir(mode=0o750,exist_ok=True)
shutil.copytree(release/'runtime',candidate)
expected=json.loads((release/'data-evidence.json').read_text())
assert evidence(candidate/'orders.sqlite3')==expected, 'Transferred rows differ'
# Prove independent restoration using a second isolated copy.
restore=Path('/var/lib/newbot/restore-verification')/release.name
restore.parent.mkdir(mode=0o750,exist_ok=True)
shutil.copytree(candidate,restore)
assert evidence(restore/'orders.sqlite3')==expected
user=pwd.getpwnam('newbot')
for p in [release,*release.rglob('*'),candidate.parent,restore.parent]:
    os.chown(p,p.stat().st_uid,user.pw_gid)
    if p.is_dir():os.chmod(p,0o750)
    else:os.chmod(p,p.stat().st_mode | 0o040)
for directory in [candidate,restore,release/'legacy_app']:
    for p in [directory,*directory.rglob('*')]:
        os.chown(p,user.pw_uid,user.pw_gid)
        if p.is_file():os.chmod(p,0o600 if p.suffix in ('.json','.env','.sqlite3','.key') else 0o644)
current=Path('/opt/newbot/current')
assert not current.exists() and not current.is_symlink(), 'Existing release: stop instead of replacing'
current.symlink_to(release,target_is_directory=True)
deploy=release/'deployment/newbot'
etc=Path('/etc/newbot');etc.mkdir(mode=0o750,exist_ok=True)
shutil.copyfile(candidate/'runtime.env',etc/'runtime.env')
os.chown(etc/'runtime.env',0,user.pw_gid);os.chmod(etc/'runtime.env',0o640)
for name in ['newbot-backend.service','newbot-protect.service','newbot-protect.timer','newbot-backup.service','newbot-backup.timer']:
    shutil.copyfile(deploy/name,Path('/etc/systemd/system')/name)
for name in ['newbot-protect.service','newbot-backup.service']:
    drop=Path('/etc/systemd/system')/(name+'.d');drop.mkdir(exist_ok=True)
    (drop/'candidate.conf').write_text('[Service]\nEnvironment=NEWBOT_PROTECTION_DB='+str(candidate/'orders.sqlite3')+'\n')
Path('/etc/systemd/journald.conf.d').mkdir(exist_ok=True)
shutil.copyfile(deploy/'journald-newbot.conf','/etc/systemd/journald.conf.d/60-newbot-limits.conf')
shutil.copyfile(deploy/'logrotate-newbot.conf','/etc/logrotate.d/newbot')
# Only replace the fresh server's package default, keep recoverable copy.
shutil.copyfile('/etc/logrotate.d/nginx','/var/backups/newbot-deployment/nginx-logrotate-original')
shutil.copyfile(deploy/'logrotate-nginx.conf','/etc/logrotate.d/nginx')
shutil.copyfile(deploy/'nginx-maintenance.conf','/etc/nginx/sites-available/newbot')
site=Path('/etc/nginx/sites-enabled/newbot');site.symlink_to('/etc/nginx/sites-available/newbot')
default=Path('/etc/nginx/sites-enabled/default')
if default.is_symlink():
    shutil.copyfile(default.resolve(),'/var/backups/newbot-deployment/nginx-default-original')
    default.unlink()
subprocess.run(['nginx','-t'],check=True)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','restart','systemd-journald'],check=True)
subprocess.run(['systemctl','reload','nginx'],check=True)
result=dict(candidate=str(candidate),restore=str(restore),evidence=expected,live_database_switched=False,bot_started=False)
(candidate/'migration-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(dict(candidate=str(candidate),tables=expected['table_count'],stock=expected['stock_quantity'],users=expected['user_count'],restore_verified=True,live_database_switched=False)))
