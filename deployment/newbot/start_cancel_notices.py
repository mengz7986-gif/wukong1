"""Dedicated future cancellation/account notices, no business workers."""
import fcntl
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv('/etc/newbot/runtime.env', override=True)
root = Path('/opt/newbot/current/legacy_app')
sys.path.insert(0, str(root))
os.chdir(root)
from cancel_notice_worker import tick, timestamp, authorize, error_category
from future_account_notice_worker import tick as account_tick, PREDICATE
from telegram_delivery_sync import send_message

policy = json.loads(Path('/etc/newbot/cancel-notices-policy.json').read_text())
assert policy['version'] == 1 and policy['enabled'] is True
assert policy['bot_id'] == '8729718308'
timestamp(policy['activated_at'])
database = Path(policy['database']).resolve()
assert database == Path('/var/lib/newbot/live/release-ca85b647be4649b7ac839729b45dedfc/orders.sqlite3')
token = os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN') or ''
assert token.split(':')[0] == policy['bot_id']
accounts_enabled = policy.get('future_account_notices_enabled', False)
assert type(accounts_enabled) is bool
if sys.argv[1:] == ['--check-only']:
    # Startup/environment smoke check with a read-only connection and NO send.
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0.25) as probe:
        probe.execute('''SELECT count(*) FROM telegram_notification_events
            WHERE kind='cancelled' AND status='pending'
            AND julianday(created_at)>julianday(?)''', (policy['activated_at'],)).fetchone()
        if accounts_enabled:
            probe.execute('SELECT count(*) FROM telegram_notification_events WHERE '+PREDICATE,
                          (policy['activated_at'],)).fetchone()
    print('CANCEL_NOTICE_READ_ONLY_STARTUP_CHECK_OK', flush=True)
    raise SystemExit(0)
assert not sys.argv[1:]
lock = open('/var/lib/newbot/cancel-notices.lock', 'a')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
connection = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=0.25)
connection.set_authorizer(authorize)
print('CANCEL_NOTICE_WORKER_READY', flush=True)
if accounts_enabled:
    print('FUTURE_ACCOUNT_NOTICE_WORKER_READY', flush=True)
while True:
    try:
        result = tick(connection, policy['activated_at'], lambda payload: send_message(token, payload))
        if result not in ('idle', 'paused'):
            print('CANCEL_NOTICE_RESULT ' + result, flush=True)
        if accounts_enabled:
            result = account_tick(connection, policy['activated_at'], lambda payload: send_message(token, payload))
            if result not in ('idle', 'paused'):
                print('ACCOUNT_NOTICE_RESULT ' + result, flush=True)
    except Exception as error:
        # No customer payload, credentials, provider URL or exception strings.
        print('CANCEL_NOTICE_WORKER_CHECK_FAILED kind=' + error_category(error), flush=True)
        raise SystemExit(1) from None
    time.sleep(2)
