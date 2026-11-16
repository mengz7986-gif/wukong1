"""Apply the existing additive migrations without creating demo business data."""
import importlib


def migrate():
    import api
    api.ensure_business_schema()
    modules = (
        'telegram_accounts', 'telegram_order_queue', 'execution_safety',
        'inventory_operations', 'bundles', 'bundle_inventory',
        'warehouse_allocation', 'pending_work_actions', 'queue_recovery_admin',
    )
    with api.db.engine.begin() as connection:
        for name in modules:
            importlib.import_module(name).migrate(connection)


if __name__ == '__main__':
    migrate()
    print('Local schema migrations complete; no demo data created.')
