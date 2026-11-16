"""Explicit, transactional metadata-only migration. Never changes price/cost rows."""
def migrate(connection):
    columns={r[1] for r in connection.execute('PRAGMA table_info(products)')}
    if 'reference_price_state' not in columns:
        connection.execute('ALTER TABLE products ADD COLUMN reference_price_state VARCHAR(20)')


def rollback(connection):
    columns={r[1] for r in connection.execute('PRAGMA table_info(products)')}
    if 'reference_price_state' in columns:
        connection.execute('ALTER TABLE products DROP COLUMN reference_price_state')
