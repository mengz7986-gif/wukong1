"""Evidence-backed operator review, never a sender or business-state writer.

A reviewed original send remains UNKNOWN. A separately acknowledged final
receipt can close the *deployment review*, not the original delivery outcome.
Only exact, root-protected reviewed fingerprints qualify; SENDING never does.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

REVIEW_PATH = Path('/etc/newbot/receipt-reviewed-outcomes.json')
REVIEW_V2_EXACT_SCOPE = {
    'ca0c527ded2e4ab499b409569ce4ad55':'2026-10-23-E1023-1030',
    '329f96c85a614ecd87bc823c3c800125':'2026-10-24-F1024-978',
    'b2fa6eb30a8345288b3190570893e65a':'2026-10-24-F1024-1016',
    '49a1e30dc39647a5b8eee75a242acccc':'2026-10-24-F1024-1017',
}
REVIEW_V3_EXACT_SCOPE = dict(REVIEW_V2_EXACT_SCOPE, **{
    '5539c922040546129825a91eddea67a9':'2026-10-24-F1024-1054',
    '16b00668451d49c8846e909e84b9659b':'2026-10-24-F1024-1058',
})
REVIEW_V4_EXACT_SCOPE = dict(REVIEW_V3_EXACT_SCOPE, **{
    '77e5db78896142f097e17cd9605aef91':'2026-10-24-F1024-1110',
})
REVIEW_V5_EXACT_SCOPE = dict(REVIEW_V4_EXACT_SCOPE, **{
    'ed031bbb6f894cbe919ba84af2c945fa':'2026-10-24-F1024-1115',
    '8c46fdb1f3144e99af3f49dd620603d4':'2026-10-24-F1024-1154',
    '1d6f6f8d4eb1477ba60c35308b043644':'2026-10-24-F1024-1166',
    '8baa99658fae472b840e07f93f662055':'2026-10-24-F1024-1203',
    '001f16f479484649ad41dde0e1ecaf2c':'2026-10-24-F1024-1235',
    '62b7a8b55e1649a580dfb98ea32bd56e':'2026-10-24-F1024-1260',
})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def validate(review, unresolved, connection):
    if review.get('version') not in (1,2,3,4,5) or review.get('approval_state') != 'APPROVED':
        raise ValueError('RECEIPT_REVIEW_APPROVAL_REQUIRED')
    records = review.get('records')
    if not isinstance(records, list) or not records:
        raise ValueError('RECEIPT_REVIEW_EXACT_SCOPE_REQUIRED')
    if review['version']==1 and len(records)!=1:
        raise ValueError('RECEIPT_REVIEW_EXACT_SCOPE_REQUIRED')
    if review['version']==2 and (len(records)!=len(REVIEW_V2_EXACT_SCOPE) or
            {r.get('attempt_id'):r.get('order_number') for r in records}!=REVIEW_V2_EXACT_SCOPE):
        raise ValueError('RECEIPT_REVIEW_EXACT_SCOPE_REQUIRED')
    if review['version']==3 and (len(records)!=len(REVIEW_V3_EXACT_SCOPE) or
            {r.get('attempt_id'):r.get('order_number') for r in records}!=REVIEW_V3_EXACT_SCOPE):
        raise ValueError('RECEIPT_REVIEW_EXACT_SCOPE_REQUIRED')
    if review['version']==4 and (len(records)!=len(REVIEW_V4_EXACT_SCOPE) or
            {r.get('attempt_id'):r.get('order_number') for r in records}!=REVIEW_V4_EXACT_SCOPE):
        raise ValueError('RECEIPT_REVIEW_EXACT_SCOPE_REQUIRED')
    if review['version']==5 and (len(records)!=len(REVIEW_V5_EXACT_SCOPE) or
            {r.get('attempt_id'):r.get('order_number') for r in records}!=REVIEW_V5_EXACT_SCOPE):
        raise ValueError('RECEIPT_REVIEW_EXACT_SCOPE_REQUIRED')
    approved = {row['attempt_id']: row for row in records}
    if len(approved) != len(records):
        raise ValueError('RECEIPT_REVIEW_DUPLICATE_ATTEMPT')
    resolved = []
    for attempt in unresolved:
        record = approved.get(attempt['attempt_id'])
        if attempt['status'] != 'UNKNOWN' or record is None:
            raise ValueError('RECEIPT_REVIEW_UNAPPROVED_ATTEMPT')
        if (record.get('original_send_status') != 'UNKNOWN'
                or record.get('final_ack_is_not_original_send_success') is not True
                or record.get('no_resend') is not True
                or not record.get('user_authorization')
                or record.get('original_attempt_sha256') != digest(attempt)):
            raise ValueError('RECEIPT_REVIEW_ORIGINAL_EVIDENCE_MISMATCH')
        numbers = attempt.get('order_numbers')
        if numbers != [record.get('order_number')]:
            raise ValueError('RECEIPT_REVIEW_ORDER_SCOPE_MISMATCH')
        connection.row_factory = sqlite3.Row
        order = connection.execute('''SELECT id,source_chat_id,source_message_id
            FROM orders WHERE order_number=?''', (record['order_number'],)).fetchone()
        if order is None:
            raise ValueError('RECEIPT_REVIEW_ORDER_MISSING')
        commits = connection.execute('''SELECT namespace,chat_id,message_id FROM telegram_execution_commits
            WHERE order_id=?''', (order['id'],)).fetchall()
        if len(commits) != 1:
            raise ValueError('RECEIPT_REVIEW_COMMIT_NOT_UNIQUE')
        commit = commits[0]
        identity = hashlib.sha256(('%s:%s:%s' % tuple(commit)).encode()).hexdigest()
        if identity[:16] != attempt.get('task_id'):
            raise ValueError('RECEIPT_REVIEW_TASK_MISMATCH')
        jobs = connection.execute('''SELECT state,lease_token,lease_until FROM telegram_order_jobs
            WHERE namespace=? AND chat_id=? AND message_id=?''', tuple(commit)).fetchall()
        if len(jobs) != 1 or jobs[0]['state'] != 'COMPLETED' or jobs[0]['lease_token'] is not None or jobs[0]['lease_until'] is not None:
            raise ValueError('RECEIPT_REVIEW_TASK_NOT_QUIET')
        if str(commit['chat_id']) != str(order['source_chat_id']) or commit['message_id'] != order['source_message_id']:
            raise ValueError('RECEIPT_REVIEW_SOURCE_MISMATCH')
        receipt = connection.execute('SELECT * FROM telegram_execution_receipts WHERE event_key=?',
            (record['final_event_key'],)).fetchone()
        if (receipt is None or receipt['event_key'] != 'final:' + identity + ':0'
                or receipt['status'] != 'sent' or receipt['attempts'] != 1
                or type(receipt['delivered_message_id']) is not int or receipt['delivered_message_id'] <= 0
                or receipt['delivered_message_id'] != record['final_delivered_message_id']
                or receipt['error_code'] is not None
                or str(receipt['chat_id']) != str(commit['chat_id'])
                or receipt['source_message_id'] != commit['message_id']
                or digest(dict(receipt)) != record['final_receipt_sha256']):
            raise ValueError('RECEIPT_REVIEW_FINAL_ACK_MISMATCH')
        # A multi-chunk receipt must not be partially acknowledged.
        chunks = connection.execute('SELECT event_key,status FROM telegram_execution_receipts WHERE event_key LIKE ?',
            ('final:' + identity + ':%',)).fetchall()
        if len(chunks) != 1:
            raise ValueError('RECEIPT_REVIEW_MULTI_CHUNK_NOT_APPROVED')
        resolved.append(attempt['attempt_id'])
    return resolved


def reviewed_unresolved(unresolved):
    if not REVIEW_PATH.exists():
        raise ValueError('RECEIPT_REVIEW_MISSING')
    info = REVIEW_PATH.lstat()
    parent = REVIEW_PATH.parent.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or parent.st_uid != 0 or parent.st_mode & 0o022):
        raise ValueError('RECEIPT_REVIEW_UNTRUSTED_FILE')
    database = os.environ.get('DATABASE_URL', '')
    if not database.startswith('sqlite:////var/lib/newbot/live/'):
        raise ValueError('RECEIPT_REVIEW_DATABASE_IDENTITY_REQUIRED')
    review = json.loads(REVIEW_PATH.read_text())
    with sqlite3.connect(Path(database.removeprefix('sqlite:///')).as_uri() + '?mode=ro', uri=True, timeout=2) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        from connect_receipt_hold import partition_verified as partition_connect
        unresolved,connect_review=partition_connect(unresolved,connection)
        from timeout_receipt_hold import partition_verified as partition_timeout
        unresolved, timeout_review = partition_timeout(unresolved, connection)
        from six_unknown_receipt_hold import partition_verified
        unresolved, special = partition_verified(unresolved, connection)
        approved = {row['attempt_id'] for row in review.get('records', [])}
        original = [row for row in unresolved if row['attempt_id'] in approved]
        frozen = [row for row in unresolved if row['attempt_id'] not in approved]
        result = validate(review, original, connection)
        from frozen_direct_receipts import POLICY_PATH, reviewed_frozen
        if frozen or POLICY_PATH.exists():
            result += reviewed_frozen(frozen, connection)
        return result + special + timeout_review + connect_review
