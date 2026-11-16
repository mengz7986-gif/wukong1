import copy
import hashlib
from pathlib import Path
import sqlite3
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'legacy_app'))
from frozen_direct_receipts import validate,digest,terminal_links
class DirectHold(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
  self.db.executescript('''CREATE TABLE orders(id INTEGER,order_number TEXT,source_chat_id TEXT,source_message_id INTEGER);
   CREATE TABLE telegram_execution_commits(order_id INTEGER,namespace TEXT,chat_id TEXT,message_id INTEGER);
   CREATE TABLE telegram_order_jobs(namespace TEXT,chat_id TEXT,message_id INTEGER,state TEXT,lease_until REAL,lease_token TEXT);
   INSERT INTO orders VALUES(1,'TEST','-123',42);
   INSERT INTO telegram_execution_commits VALUES(1,'shared','-123',42);
   INSERT INTO telegram_order_jobs VALUES('shared','-123',42,'COMPLETED',NULL,NULL);''')
  task=hashlib.sha256(b'shared:-123:42').hexdigest()[:16]
  self.rows=[dict(attempt_id='test-'+str(i),status='UNKNOWN',order_numbers=['TEST'],task_id=task,message_id=None) for i in range(73)]
  self.policy=dict(version=1,no_resend=True,original_status='UNKNOWN',authorization='user_approved_expired_messages_ignore_and_release_20261102',records=[dict(attempt_ref=hashlib.sha256(r['attempt_id'].encode()).hexdigest(),attempt_sha256=digest(r),links_sha256=digest(terminal_links(self.db,r))) for r in self.rows])
 def tearDown(self):self.db.close()
 def test_exact_hold_passes_without_writes(self):
  before=self.db.total_changes;self.assertEqual(len(validate(self.policy,self.rows,self.db)),73);self.assertEqual(self.db.total_changes,before)
 def test_extra_attempt_rejected(self):
  with self.assertRaisesRegex(ValueError,'EXACT_SET_CHANGED'):validate(self.policy,self.rows+[dict(self.rows[0],attempt_id='extra')],self.db)
 def test_missing_attempt_rejected(self):
  with self.assertRaisesRegex(ValueError,'EXACT_SET_CHANGED'):validate(self.policy,self.rows[:-1],self.db)
 def test_sending_or_changed_attempt_rejected(self):
  rows=copy.deepcopy(self.rows);rows[0]['status']='SENDING'
  with self.assertRaisesRegex(ValueError,'ATTEMPT_CHANGED'):validate(self.policy,rows,self.db)
 def test_live_task_rejected(self):
  self.db.execute("UPDATE telegram_order_jobs SET state='PROCESSING',lease_until=999")
  with self.assertRaisesRegex(ValueError,'TASK_NOT_TERMINAL'):validate(self.policy,self.rows,self.db)
 def test_source_reassignment_rejected(self):
  self.db.execute("UPDATE orders SET source_chat_id='-999'")
  with self.assertRaisesRegex(ValueError,'SOURCE_CHANGED'):validate(self.policy,self.rows,self.db)
 def test_no_resend_authorization_required(self):
  self.policy['no_resend']=False
  with self.assertRaisesRegex(ValueError,'AUTHORIZATION_REQUIRED'):validate(self.policy,self.rows,self.db)
if __name__=='__main__':unittest.main()
