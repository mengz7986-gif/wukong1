"""Exact historical UNKNOWN hold; never changes status or sends messages."""
import hashlib
import json
from pathlib import Path
import sqlite3
import stat

POLICY_PATH = Path('/etc/newbot/frozen-direct-receipts-20261102.json')
POLICY_SHA256 = 'a60ddcc3511fa2d1dbb3e2c9455d1f62c34624b35ecfced0f37a6cc6943ae429'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def terminal_links(connection, attempt):
    connection.row_factory = sqlite3.Row
    orders, commits, jobs = [], [], []
    for number in attempt.get('order_numbers', []):
        order = connection.execute('SELECT id,source_chat_id,source_message_id FROM orders WHERE order_number=?', (number,)).fetchone()
        if order is None:
            raise ValueError('FROZEN_DIRECT_ORDER_MISSING')
        orders.append(dict(order))
        links = [dict(r) for r in connection.execute('SELECT namespace,chat_id,message_id FROM telegram_execution_commits WHERE order_id=?', (order['id'],))]
        if len(links) != 1:
            raise ValueError('FROZEN_DIRECT_COMMIT_NOT_UNIQUE')
        link = links[0]
        identity = '%s:%s:%s' % (link['namespace'], link['chat_id'], link['message_id'])
        if (hashlib.sha256(identity.encode()).hexdigest()[:16] != attempt.get('task_id')
                or str(order['source_chat_id']) != str(link['chat_id'])
                or order['source_message_id'] != link['message_id']):
            raise ValueError('FROZEN_DIRECT_SOURCE_CHANGED')
        commits += links
        row = connection.execute('SELECT state,lease_until,lease_token FROM telegram_order_jobs WHERE namespace=? AND chat_id=? AND message_id=?',
                                 (link['namespace'], link['chat_id'], link['message_id'])).fetchall()
        if len(row) != 1 or row[0]['state'] != 'COMPLETED' or row[0]['lease_until'] is not None or row[0]['lease_token'] is not None:
            raise ValueError('FROZEN_DIRECT_TASK_NOT_TERMINAL')
        jobs.append(dict(row[0]))
    if not orders:
        raise ValueError('FROZEN_DIRECT_ORDER_SCOPE_EMPTY')
    return [orders, commits, jobs]


def validate(policy, attempts, connection):
    if (policy.get('version') != 1 or policy.get('no_resend') is not True
            or policy.get('original_status') != 'UNKNOWN'
            or policy.get('authorization') != 'user_approved_expired_messages_ignore_and_release_20261102'):
        raise ValueError('FROZEN_DIRECT_AUTHORIZATION_REQUIRED')
    records = policy.get('records', [])
    approved = {r['attempt_ref']: r for r in records}
    current = {hashlib.sha256(r['attempt_id'].encode()).hexdigest(): r for r in attempts}
    if len(records) != 73 or len(approved) != 73 or len(attempts) != 73 or set(approved) != set(current):
        raise ValueError('FROZEN_DIRECT_EXACT_SET_CHANGED')
    for key, attempt in current.items():
        record = approved[key]
        if attempt.get('status') != 'UNKNOWN' or digest(attempt) != record['attempt_sha256']:
            raise ValueError('FROZEN_DIRECT_ATTEMPT_CHANGED')
        if digest(terminal_links(connection, attempt)) != record['links_sha256']:
            raise ValueError('FROZEN_DIRECT_LINKS_CHANGED')
    return [r['attempt_id'] for r in attempts]


def reviewed_frozen(attempts, connection):
    info = POLICY_PATH.lstat()
    parent = POLICY_PATH.parent.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or parent.st_uid != 0 or parent.st_mode & 0o022:
        raise ValueError('FROZEN_DIRECT_UNTRUSTED_POLICY')
    raw = POLICY_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != POLICY_SHA256:
        raise ValueError('FROZEN_DIRECT_EXACT_POLICY_REQUIRED')
    return validate(json.loads(raw), attempts, connection)
