import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

root=Path(__file__).resolve().parents[1]
stub=types.ModuleType('sqlalchemy');stub.text=lambda sql:sql
with patch.dict(sys.modules,{'sqlalchemy':stub}):
    spec=importlib.util.spec_from_file_location('hold_under_test',root/'legacy_app/p0_recovery_protection.py')
    hold=importlib.util.module_from_spec(spec);spec.loader.exec_module(hold)
class Result:
    def __init__(self,cursor):self.rows=cursor.fetchall();self.mapping=False
    def mappings(self):self.mapping=True;return self
    def all(self):return list(self)
    def __iter__(self):return iter([dict(r) for r in self.rows] if self.mapping else self.rows)
    def scalars(self):return [r[0] for r in self.rows]
    def scalar(self):return self.rows[0][0] if self.rows else None
class Engine:
    def __init__(self,c):self.c=c
    @contextlib.contextmanager
    def connect(self):yield self
    def execute(self,sql,params=None):return Result(self.c.execute(sql,params or {}))
class ExactHold(unittest.TestCase):
    def setUp(self):
        # Unit-test the unchanged original incident checks separately; integrated
        # original29 + new7 production gate is covered by test_recovery7_hold.
        self.companion=patch('recovery_timeout_hold.partition_verified',side_effect=lambda rows,c:(rows,[]))
        self.companion.start();self.addCleanup(self.companion.stop)
        self.tmp=tempfile.TemporaryDirectory();self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE telegram_order_jobs(namespace TEXT,chat_id TEXT,message_id INTEGER,sender_id TEXT,state TEXT,received_at REAL,source_date REAL,started_at REAL,finished_at REAL,lease_until REAL,lease_token TEXT,attempts INTEGER,fingerprint TEXT,draft_token TEXT);
          CREATE TABLE orders(id INTEGER,source_chat_id TEXT,source_message_id INTEGER);
          CREATE TABLE telegram_execution_commits(namespace TEXT,chat_id TEXT,message_id INTEGER,order_id INTEGER);
          CREATE TABLE telegram_order_drafts(namespace TEXT,chat_id TEXT,message_id INTEGER,token TEXT);
          CREATE TABLE telegram_execution_receipts(event_key TEXT,chat_id TEXT,source_message_id INTEGER,edit_message_id INTEGER,status TEXT,attempts INTEGER,created_at REAL,delivered_message_id INTEGER,error_code TEXT);''')
        for n in range(9):self.db.execute('INSERT INTO telegram_order_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('shared','-123',n+1,'123','PROCESSING' if n<7 else 'RECEIVED',100.,100.,100.,None,120. if n<7 else None,'frozen' if n<7 else None,1,'test',None))
        for n in range(29):self.db.execute('INSERT INTO telegram_execution_receipts VALUES(?,?,?,?,?,?,?,?,?)',(f'event-{n:02}','-123',n+100,None,'unknown',1,100.,None,'delivery_unknown'))
        self.entries=[]
        for row in self.db.execute('SELECT * FROM telegram_order_jobs'):
            entry=hold.job_pin(dict(row));entry.update(commit_order_ids=[],source_order_ids=[],draft_token_sha256=[]);self.entries.append(entry)
        self.path=Path(self.tmp.name)/'policy.json'
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def policy(self,count=29):
        rows=[dict(r) for r in self.db.execute("SELECT * FROM telegram_execution_receipts WHERE status IN ('sending','unknown') ORDER BY event_key")][:count]
        p=dict(version=1,incident=hold.INCIDENT,bot_id=hold.BOT_ID,cutoff_received_at=123.,entries=self.entries,frozen_execution_receipts_sha256=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest())
        raw=json.dumps(p).encode();self.path.write_bytes(raw);hold.PINNED_POLICY_SHA256=hashlib.sha256(raw).hexdigest()
        return hold.Protection(self.path)
    def verify(self,p):p.verify_before_workers(types.SimpleNamespace(engine=Engine(self.db)))
    def test_original_fifteen_does_not_silently_admit_fourteen_more(self):
        p=self.policy(15)
        with self.assertRaisesRegex(hold.ProtectionError,'FROZEN_RECEIPT_SET_CHANGED'):self.verify(p)
    def test_exact_twenty_nine_and_nine_jobs_verify_without_writes(self):
        p=self.policy();before=self.db.total_changes;self.verify(p)
        self.assertTrue(p.verified);self.assertEqual(self.db.total_changes,before)
    def test_any_additional_unknown_is_rejected(self):
        p=self.policy();self.db.execute("INSERT INTO telegram_execution_receipts VALUES('extra','-123',999,NULL,'unknown',1,100,NULL,'delivery_unknown')")
        with self.assertRaisesRegex(hold.ProtectionError,'FROZEN_RECEIPT_SET_CHANGED'):self.verify(p)
    def test_changed_original_receipt_is_rejected(self):
        p=self.policy();self.db.execute("UPDATE telegram_execution_receipts SET error_code='changed' WHERE event_key='event-00'")
        with self.assertRaisesRegex(hold.ProtectionError,'FROZEN_RECEIPT_SET_CHANGED'):self.verify(p)
    def test_changed_original_job_is_rejected(self):
        p=self.policy();self.db.execute('UPDATE telegram_order_jobs SET attempts=2 WHERE message_id=1')
        with self.assertRaisesRegex(hold.ProtectionError,'FROZEN_JOB_CHANGED'):self.verify(p)
    def test_unpinned_policy_is_rejected(self):
        self.policy();self.path.write_bytes(self.path.read_bytes()+b' ')
        with self.assertRaisesRegex(hold.ProtectionError,'EXACT_POLICY_SHA_REQUIRED'):hold.Protection(self.path)
if __name__=='__main__':unittest.main()
