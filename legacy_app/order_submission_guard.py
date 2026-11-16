"""Persistent fail-closed order-insert fence understood by old SQLite clients.

Control state only. Never alter an order, stock, price or accounting row.
No automatic unblock: recovery requires a separately verified procedure.
"""
import re,time

def _execute(connection,sql,args=()):
    method=getattr(connection,'exec_driver_sql',None)
    return method(sql,args) if method else connection.execute(sql,args)

def migrate(connection):
    existed=_execute(connection,"SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_submission_safety'").fetchone() is not None
    prior_trigger=_execute(connection,"SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='order_submission_safety_insert'").fetchone() is not None
    if prior_trigger and not existed:raise RuntimeError('SUBMISSION_HOLD_CONTROL_TABLE_MISSING')
    _execute(connection,"""CREATE TABLE IF NOT EXISTS order_submission_safety (
      id INTEGER PRIMARY KEY CHECK(id=1), blocked INTEGER NOT NULL CHECK(blocked IN (0,1)),
      reason TEXT, owner TEXT, updated_at REAL)""")
    if not existed:_execute(connection,'INSERT INTO order_submission_safety(id,blocked) VALUES(1,0)')
    elif _execute(connection,'SELECT 1 FROM order_submission_safety WHERE id=1').fetchone() is None:
        raise RuntimeError('SUBMISSION_HOLD_CONTROL_ROW_MISSING')
    _execute(connection,"""CREATE TRIGGER IF NOT EXISTS order_submission_safety_insert
      BEFORE INSERT ON orders
      WHEN COALESCE((SELECT blocked FROM order_submission_safety WHERE id=1),1)!=0
      BEGIN SELECT RAISE(ABORT,'ORDER_SUBMISSION_BLOCKED_PRICE_COMPATIBILITY'); END""")

def block(connection,owner):
    if not isinstance(owner,str) or not re.fullmatch('[a-f0-9]{32}',owner):raise ValueError('SUBMISSION_HOLD_OWNER_INVALID')
    migrate(connection)
    row=_execute(connection,'SELECT blocked,owner FROM order_submission_safety WHERE id=1').fetchone()
    if row[0] and row[1]!=owner:raise RuntimeError('SUBMISSION_HOLD_OWNED_BY_OTHER_OPERATION')
    _execute(connection,"UPDATE order_submission_safety SET blocked=1,reason='ROLLBACK_PRICE_SEMANTICS_UNVERIFIED',owner=?,updated_at=? WHERE id=1",(owner,time.time()))

def snapshot(connection):
    row=_execute(connection,'SELECT blocked,reason,owner FROM order_submission_safety WHERE id=1').fetchone()
    if row is None:raise RuntimeError('SUBMISSION_HOLD_STATE_MISSING')
    return dict(blocked=bool(row[0]),reason=row[1],owner=row[2])
