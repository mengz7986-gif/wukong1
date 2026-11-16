"""Additive, rollback-compatible order recording/export audit migration."""

from sqlalchemy import text


def migrate(connection):
    dialect = connection.engine.dialect.name
    if dialect == "sqlite":
        columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(orders)").fetchall()
        }
    else:
        columns = {
            row[0]
            for row in connection.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='orders'"
            )).fetchall()
        }
    additions = {
        "business_order_number": "VARCHAR(50)",
        "recorded_at": "DATETIME",
        "recorded_export_batch_id": "VARCHAR(64)",
    }
    for name, sql_type in additions.items():
        if name not in columns:
            connection.execute(text(f"ALTER TABLE orders ADD COLUMN {name} {sql_type}"))

    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_orders_business_order_number "
        "ON orders (business_order_number)"
    ))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_orders_recorded_at ON orders (recorded_at)"
    ))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_orders_recorded_export_batch_id "
        "ON orders (recorded_export_batch_id)"
    ))

