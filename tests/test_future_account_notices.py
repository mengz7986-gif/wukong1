import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'legacy_app'))
from future_account_notice_worker import claim, tick
from cancel_notice_worker import authorize, claim as cancel_claim

CUTOFF='2026-11-01T12:00:00+00:00'
FUTURE='2026-11-01T12:00:01+00:00'

class Notices(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'RAINBOW_DEPLOY_DRAIN_PATH':str(Path(self.temp.name)/'drain.json')})
        self.env.start()
        self.db=sqlite3.connect(Path(self.temp.name)/'test.db')
        self.db.execute('''CREATE TABLE telegram_notification_events(event_key TEXT PRIMARY KEY,
            user_id INTEGER,chat_id TEXT,kind TEXT,payload TEXT,created_at TEXT,
            status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,last_error TEXT,sent_at TEXT)''')
        self.db.execute('CREATE TABLE business_guard(value TEXT)')
        self.db.execute("INSERT INTO business_guard VALUES('unchanged')")
        self.db.commit()
    def tearDown(self):
        self.db.close();self.env.stop();self.temp.cleanup()
    def add(self,key,kind='receivable',created=FUTURE,status='pending',attempts=0):
        self.db.execute('''INSERT INTO telegram_notification_events
            (event_key,user_id,chat_id,kind,payload,created_at,status,attempts)
            VALUES(?,1,'-123',?,?,?,?,?)''',
            (key,kind,json.dumps({'text':'fictional test'}),created,status,attempts))
        self.db.commit()
    def test_cutoff_and_history_are_untouched(self):
        for key,when in [('old','2026-10-31T23:00:00+00:00'),('equal',CUTOFF),('new',FUTURE)]:self.add(key,created=when)
        before=[tuple(r) for r in self.db.execute("SELECT * FROM telegram_notification_events WHERE event_key!='new'")]
        sent=[];self.db.set_authorizer(authorize)
        self.assertEqual(tick(self.db,CUTOFF,lambda p:(sent.append(p) or True,'ok')),'sent')
        self.assertEqual(len(sent),1)
        self.assertEqual(before,[tuple(r) for r in self.db.execute("SELECT * FROM telegram_notification_events WHERE event_key!='new'")])
        self.assertEqual(tick(self.db,CUTOFF,lambda p:self.fail('duplicate')),'idle')
    def test_allowed_kinds_only(self):
        self.add('cancel','cancelled');self.add('balance','balance')
        self.assertIsNone(claim(self.db,CUTOFF))
        for kind in ['receivable','payment','prepaid','manual_bill']:
            self.add(kind,kind)
            self.assertEqual(claim(self.db,CUTOFF)[0],kind)
    def test_no_retry_sending_unknown_failed_alias_or_attempted(self):
        for state in ['sending','unknown','failed','canonical_alias','sent']:
            self.add(state,status=state,attempts=1)
        self.add('attempted',attempts=1)
        self.assertIsNone(claim(self.db,CUTOFF))
    def test_competing_claim_cannot_duplicate_api_claim(self):
        self.add('one','payment')
        other=sqlite3.connect(Path(self.temp.name)/'test.db')
        try:
            other.execute("UPDATE telegram_notification_events SET status='sending',attempts=1 WHERE event_key='one' AND status='pending'");other.commit()
            self.assertIsNone(claim(self.db,CUTOFF))
        finally:other.close()
    def test_ambiguous_send_is_frozen(self):
        self.add('one')
        self.db.set_authorizer(authorize)
        self.assertEqual(tick(self.db,CUTOFF,lambda p:(False,'telegram_timeout_delivery_unknown')),'unknown')
        self.assertEqual(tick(self.db,CUTOFF,lambda p:self.fail('must not replay')),'idle')
    def test_definitive_failure_is_not_retried(self):
        self.add('one')
        self.assertEqual(tick(self.db,CUTOFF,lambda p:(False,'telegram_http_403')),'failed')
        self.assertEqual(tick(self.db,CUTOFF,lambda p:self.fail('must not replay')),'idle')
    def test_pause_prevents_send_and_claim(self):
        self.add('one')
        from deployment_drain import pause
        pause('a'*32)
        self.assertEqual(tick(self.db,CUTOFF,lambda p:self.fail('paused')),'paused')
        self.assertEqual(self.db.execute('SELECT attempts FROM telegram_notification_events').fetchone()[0],0)
    def test_authorizer_blocks_business_writes(self):
        self.db.set_authorizer(authorize)
        with self.assertRaises(sqlite3.DatabaseError):self.db.execute("UPDATE business_guard SET value='bad'")
        self.assertEqual(self.db.execute('SELECT value FROM business_guard').fetchone()[0],'unchanged')
    def test_invalid_timezone_is_rejected(self):
        with self.assertRaises(ValueError):claim(self.db,'2026-11-01T12:00:00')
    def test_cancellation_cutoff_preserves_old_events(self):
        self.db.executescript('''CREATE TABLE orders(id INTEGER,user_id INTEGER,status TEXT,order_number TEXT,source_chat_id TEXT,source_message_id INTEGER);
            CREATE TABLE users(id INTEGER,telegram_id TEXT);
            CREATE TABLE order_cancellation_facts(event_id TEXT,order_id INTEGER,cancelled_at TEXT,reason TEXT);
            INSERT INTO users VALUES(1,'123');
            INSERT INTO orders VALUES(1,1,'cancelled','TEST','-123',42);''')
        for key,when in [('old',CUTOFF),('new',FUTURE)]:
            self.add('cancel:'+key,'cancelled',when)
            self.db.execute('INSERT INTO order_cancellation_facts VALUES(?,1,?,?)',(key,when,'test cancellation'))
            self.db.execute('UPDATE telegram_notification_events SET payload=? WHERE event_key=?',
                (json.dumps({'text':'❌ 订单已取消\n\n订单号：TEST\n取消原因：test cancellation','reply_to_message_id':42}),'cancel:'+key))
            self.db.commit()
        self.db.set_authorizer(authorize)
        self.assertEqual(cancel_claim(self.db,CUTOFF)[0],'cancel:new')
        self.assertEqual(self.db.execute("SELECT status,attempts FROM telegram_notification_events WHERE event_key='cancel:old'").fetchone()[0],'pending')
        self.assertIsNone(cancel_claim(self.db,CUTOFF))

if __name__=='__main__':unittest.main()
