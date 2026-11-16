"""Candidate entrypoint: independent split releases, NO notification worker."""
import fcntl,os,sys,time
from pathlib import Path
from dotenv import load_dotenv

def main():
    load_dotenv('/etc/newbot/runtime.env',override=True)
    if os.getenv('RAINBOW_DISABLE_BACKGROUND_WORKERS')!='1':raise ValueError('OTHER_WORKERS_MUST_STAY_DISABLED')
    if not os.getenv('RAINBOW_DEPLOY_DRAIN_PATH'):raise ValueError('EXPLICIT_SHARED_DRAIN_REQUIRED')
    # Override role only for this isolated process, never the shared env file.
    os.environ['NEWBOT_ROLE']='split-release-only'
    os.environ['NEWBOT_SPLIT_MAPPINGS_ONLY']='1'
    root=Path('/opt/newbot/current/legacy_app');os.chdir(root);sys.path.insert(0,str(root))
    from newbot_split_dispatch import load_policy,database_adapter,try_dispatch_once
    from telegram_source_identity import account_cutover
    import config
    from split_management import _validate_newbot_daily_policy
    config.load_runtime_settings();_validate_newbot_daily_policy(config)
    boundary=account_cutover()
    if boundary is None:raise ValueError('NEWBOT_ACCOUNT_BOUNDARY_REQUIRED')
    token=os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN','')
    database=Path(os.environ['DATABASE_URL'].removeprefix('sqlite:///')).resolve()
    if not str(database).startswith('/var/lib/newbot/live/'):raise ValueError('NEWBOT_DATABASE_SCOPE')
    check=sys.argv[1:]==['--check-only']
    if sys.argv[1:] and not check:raise ValueError('SPLIT_ARGUMENTS')
    def policy():
        return load_policy('/etc/newbot/split-release-policy.json',os.environ['NEWBOT_SPLIT_POLICY_SHA256'],
            bot_id=token.partition(':')[0],database=database,boundary=boundary.isoformat(),require_enabled=not check)
    if check:
        policy()
        import sqlite3
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as c:
            c.execute('SELECT id,status,recorded_at FROM orders LIMIT 0')
            c.execute('SELECT order_id FROM split_release_days LIMIT 0')
            c.execute('SELECT order_id FROM address_order_release_slots LIMIT 0')
        print('SPLIT_RELEASE_CHECK_ONLY_OK',flush=True);return
    # Single process guard, plus the existing per-tick SQLite writer lock and
    # shared maintenance activity admission inside tick. No autostart here.
    fd=os.open('/var/lib/newbot/split-release.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    adapter=database_adapter(database)
    try:
        while True:
            current=policy()  # File drift or disabled policy stops before tick.
            result=try_dispatch_once(adapter,current)
            if result['state']=='busy':print('SPLIT_RELEASE_BUSY_YIELD',flush=True)
            time.sleep(15)
    finally:adapter.engine.dispose();os.close(fd)

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('SPLIT_RELEASE_STOPPED '+type(error).__name__,flush=True)
        raise SystemExit(1) from None
