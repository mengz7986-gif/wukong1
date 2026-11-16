"""One-time, reversible migration for Telegram source namespaces.

The migration never guesses unknown provenance.  It retains byte-for-byte
logical copies of the v1 rows and rollback is allowed only before any scoped
source row changes.
"""
from __future__ import annotations

import hashlib
import json

from telegram_source_identity import historical_scope


TABLES = (
    "telegram_order_drafts",
    "telegram_message_events",
    "telegram_order_jobs",
)


def _columns(connection, table):
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _exists(connection, table):
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def content_hash(connection, table, columns=None):
    """Return a stable content digest without putting row values in logs."""
    columns = list(columns or _columns(connection, table))
    projection = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    rows = connection.execute(f'SELECT {projection} FROM "{table}"').fetchall()
    encoded = json.dumps(sorted([list(row) for row in rows], key=repr),
                         ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _archive(connection, table):
    archive = table + "__source_v1_archive"
    connection.execute(f'DROP TABLE IF EXISTS "{archive}"')
    connection.execute(f'CREATE TABLE "{archive}" AS SELECT * FROM "{table}"')


def migrate(connection, old_bot_id, new_bot_id, cutover):
    if all(not _exists(connection, table) or "namespace" in _columns(connection, table)
           for table in TABLES):
        return {"status": "ALREADY_MIGRATED"}

    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for table in TABLES:
            if _exists(connection, table):
                _archive(connection, table)

        connection.execute("DROP TABLE IF EXISTS telegram_message_events")
        connection.execute("DROP TABLE IF EXISTS telegram_order_drafts")
        connection.execute("DROP TABLE IF EXISTS telegram_order_jobs")
        from telegram_accounts import DDL as account_ddl
        for sql in account_ddl:
            if "telegram_order_drafts" in sql or "telegram_message_events" in sql:
                connection.execute(sql)

        for table in ("telegram_order_drafts", "telegram_message_events"):
            archive = table + "__source_v1_archive"
            if not _exists(connection, archive):
                continue
            old_columns = _columns(connection, archive)
            rows = connection.execute(f'SELECT * FROM "{archive}"').fetchall()
            insert_columns = ["namespace"] + old_columns
            placeholders = ",".join("?" for _ in insert_columns)
            names = ",".join(f'"{name}"' for name in insert_columns)
            chat_index = old_columns.index("chat_id")
            created_index = old_columns.index("created_at")
            for row in rows:
                namespace = historical_scope(row[chat_index], row[created_index],
                                             str(old_bot_id), str(new_bot_id), cutover)
                connection.execute(
                    f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
                    (namespace, *row),
                )

        connection.execute("""CREATE TABLE telegram_order_jobs (
            namespace TEXT NOT NULL DEFAULT 'legacy:unknown',
            chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
            sender_id TEXT NOT NULL, payload TEXT NOT NULL,
            fingerprint TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'RECEIVED',
            received_at REAL NOT NULL, source_date REAL, started_at REAL,
            finished_at REAL, lease_token TEXT, lease_until REAL,
            attempts INTEGER NOT NULL DEFAULT 0, reason TEXT, draft_token TEXT,
            PRIMARY KEY(namespace,chat_id,message_id))""")
        connection.execute("CREATE INDEX ix_telegram_jobs_ready ON telegram_order_jobs(state,lease_until,received_at)")
        connection.execute("CREATE INDEX ix_telegram_jobs_chat_started ON telegram_order_jobs(namespace,chat_id,started_at)")
        connection.execute("CREATE INDEX ix_telegram_jobs_actor_state ON telegram_order_jobs(namespace,chat_id,sender_id,state)")
        connection.execute("CREATE INDEX ix_telegram_jobs_draft_token ON telegram_order_jobs(draft_token,received_at)")
        archive = "telegram_order_jobs__source_v1_archive"
        if _exists(connection, archive):
            old_columns = _columns(connection, archive)
            rows = connection.execute(f'SELECT * FROM "{archive}"').fetchall()
            allowed = [name for name in old_columns if name in _columns(connection, "telegram_order_jobs")]
            indexes = [old_columns.index(name) for name in allowed]
            names = ",".join(["namespace"] + [f'"{name}"' for name in allowed])
            placeholders = ",".join("?" for _ in range(len(allowed) + 1))
            chat_index = old_columns.index("chat_id")
            for row in rows:
                # v1 jobs store receipt time as epoch seconds.
                created = row[old_columns.index("received_at")]
                namespace = historical_scope(row[chat_index], created,
                                             str(old_bot_id), str(new_bot_id), cutover)
                connection.execute(
                    f'INSERT INTO telegram_order_jobs ({names}) VALUES ({placeholders})',
                    (namespace, *(row[index] for index in indexes)),
                )

        connection.execute("""CREATE TABLE IF NOT EXISTS telegram_message_observers(
            bot_id TEXT PRIMARY KEY, activated_at TEXT NOT NULL, evidence TEXT NOT NULL)""")
        connection.execute(
            "INSERT OR REPLACE INTO telegram_message_observers VALUES(?,?,?)",
            (str(new_bot_id), cutover, "verified_source_migration"),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("SOURCE_MIGRATION_FOREIGN_KEY_FAILURE")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
    return {"status": "MIGRATED_ORIGINALS_RETAINED"}


def rollback_before_traffic(connection):
    """Restore v1 only when every source row is still logically unchanged."""
    for table in TABLES:
        archive = table + "__source_v1_archive"
        if not _exists(connection, archive):
            continue
        legacy_columns = _columns(connection, archive)
        if content_hash(connection, table, legacy_columns) != content_hash(connection, archive):
            raise ValueError("LIVE_SOURCE_CHANGES")

    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        restored = 0
        for table in reversed(TABLES):
            archive = table + "__source_v1_archive"
            if not _exists(connection, archive):
                continue
            snapshot = table + "__scoped_rollback_snapshot"
            connection.execute(f'DROP TABLE IF EXISTS "{snapshot}"')
            connection.execute(f'ALTER TABLE "{table}" RENAME TO "{snapshot}"')
            connection.execute(f'ALTER TABLE "{archive}" RENAME TO "{table}"')
            restored += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
    return {"status": f"V1_SCHEMA_RESTORED_{restored}_TABLES"}
