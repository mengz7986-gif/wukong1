"""Read-only full-row fingerprints and business reconciliation for migration."""
from contextlib import closing
import hashlib,json,sqlite3
from pathlib import Path

def evidence(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('BEGIN')
        assert c.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        schema=c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        names=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        tables={}
        for name in names:
            rows=c.execute('SELECT * FROM "'+name.replace('"','""')+'" ORDER BY rowid').fetchall()
            tables[name]=dict(rows=len(rows),sha256=hashlib.sha256(repr(rows).encode()).hexdigest())
        stock=c.execute('SELECT coalesce(sum(quantity),0) FROM warehouse_stock').fetchone()[0]
        fifo=c.execute('SELECT coalesce(sum(remain_qty),0) FROM inventory_batches').fetchone()[0]
        discrepancy=c.execute('''WITH quantities AS (
            SELECT warehouse_id,product_id,quantity AS stock,0 AS fifo FROM warehouse_stock
            UNION ALL SELECT warehouse_id,product_id,0,remain_qty FROM inventory_batches)
            SELECT warehouse_id,product_id,sum(stock),sum(fifo) FROM quantities
            GROUP BY warehouse_id,product_id HAVING sum(stock)<>sum(fifo)''').fetchall()
        invalid=c.execute('SELECT count(*) FROM inventory_batches WHERE remain_qty<0 OR remain_qty>quantity').fetchone()[0]
        assert not discrepancy and not invalid, 'FIFO reconciliation failed'
        balances=c.execute('SELECT id,telegram_id,agent_level,balance FROM users ORDER BY id').fetchall()
        account=dict(total_balance=c.execute('SELECT coalesce(sum(balance),0) FROM users').fetchone()[0],
            negative_users=c.execute('SELECT count(*) FROM users WHERE balance<0').fetchone()[0],
            positive_users=c.execute('SELECT count(*) FROM users WHERE balance>0').fetchone()[0],
            transactions=tables['transactions']['rows'],payments=tables['payments']['rows'],
            manual_receivables=tables['manual_receivables']['rows'])
        grades=[list(r) for r in c.execute('SELECT agent_level,count(*) FROM users GROUP BY agent_level ORDER BY agent_level').fetchall()]
        return dict(schema_sha256=hashlib.sha256(repr(schema).encode()).hexdigest(),table_count=len(names),tables=tables,
            stock_quantity=stock,fifo_quantity=fifo,fifo_discrepancies=discrepancy,invalid_fifo_batches=invalid,
            user_count=len(balances),user_grades=grades,accounts=account,integrity='ok',foreign_keys='ok')

if __name__=='__main__':
    import sys
    print(json.dumps(evidence(sys.argv[1]),ensure_ascii=False,indent=2))
