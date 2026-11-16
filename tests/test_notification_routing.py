import sqlite3
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'legacy_app'))
from notification_routing import resolve_notification_target


class Routing(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''CREATE TABLE users(id INTEGER PRIMARY KEY,telegram_id TEXT);
            INSERT INTO users VALUES(1,'123'); INSERT INTO users VALUES(2,'456');
            CREATE TABLE telegram_account_groups(chat_id TEXT PRIMARY KEY,user_id INTEGER,active INTEGER);
            CREATE TABLE orders(id INTEGER PRIMARY KEY,user_id INTEGER,source_chat_id TEXT,
                source_chat_type TEXT,status TEXT,created_at TEXT);''')
    def tearDown(self):
        self.db.close()
    def target(self, user=1):
        return resolve_notification_target(self.db, user, lambda sql: sql)
    def order(self, chat, status='pending', created='2026-11-01 10:00:00', user=1, kind='group'):
        self.db.execute('''INSERT INTO orders(user_id,source_chat_id,source_chat_type,status,created_at)
            VALUES(?,?,?,?,?)''', (user,chat,kind,status,created))
    def test_cancelled_only_history_stays_in_group(self):
        self.order('-100','cancelled')
        self.assertEqual(self.target(),'-100')
    def test_most_recent_cancelled_group_beats_older_open_order(self):
        self.order('-100',created='2026-10-30 10:00:00')
        self.order('-200','cancelled')
        self.assertEqual(self.target(),'-200')
    def test_multiple_historical_groups_choose_latest_order(self):
        for chat in ['-100','-200','-300']:self.order(chat,'cancelled')
        self.assertEqual(self.target(),'-300')
    def test_explicit_binding_beats_newer_order_group(self):
        self.db.execute("INSERT INTO telegram_account_groups VALUES('-500',1,1)")
        self.order('-200')
        self.assertEqual(self.target(),'-500')
    def test_binding_works_without_any_orders(self):
        self.db.execute("INSERT INTO telegram_account_groups VALUES('-500',1,1)")
        self.assertEqual(self.target(),'-500')
    def test_reassigned_group_is_not_used(self):
        self.order('-100',created='2026-10-30 10:00:00')
        self.order('-200')
        self.db.execute("INSERT INTO telegram_account_groups VALUES('-200',2,1)")
        self.assertEqual(self.target(),'-100')
    def test_unbound_group_is_not_resurrected(self):
        self.order('-100',created='2026-10-30 10:00:00')
        self.order('-200')
        self.db.execute("INSERT INTO telegram_account_groups VALUES('-200',1,0)")
        self.assertEqual(self.target(),'-100')
    def test_other_accounts_orders_never_supply_target(self):
        self.order('-200',user=2)
        self.assertEqual(self.target(),'123')
    def test_private_order_does_not_replace_group(self):
        self.order('-100',created='2026-10-30 10:00:00')
        self.order('123',kind='private')
        self.assertEqual(self.target(),'-100')
    def test_private_only_account_preserves_existing_behavior(self):
        self.assertEqual(self.target(),'123')
    def test_unknown_account_has_no_target(self):
        self.assertIsNone(self.target(999))
    def test_invalid_binding_fails_closed(self):
        self.db.execute("INSERT INTO telegram_account_groups VALUES('123',1,1)")
        with self.assertRaises(ValueError):self.target()
    def test_multiple_active_bindings_fail_closed(self):
        self.db.executemany('INSERT INTO telegram_account_groups VALUES(?,1,1)',[('-100',),('-200',)])
        with self.assertRaises(ValueError):self.target()


if __name__ == '__main__':
    unittest.main()
