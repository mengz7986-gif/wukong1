"""Read-only inventory, isolated backup and restore verification.

This module deliberately has no production defaults.  Every source, database and
destination must be supplied explicitly.  It never decrypts configuration and it
never restores over an existing directory or database.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


SOURCE_SUFFIXES = {".py", ".html", ".js", ".css", ".json", ".txt"}
SOURCE_EXCLUDES = {".git", ".pytest_cache", "__pycache__", "work", "outputs", "cache"}
SECRET_MARKERS = ("KEY", "TOKEN", "PASSWORD", "SECRET", "CREDENTIAL")

BUSINESS_AREAS = {
    "catalog_alias_rules": ("product", "catalog", "alias", "rule", "warehouse"),
    "inventory_fifo_audit": ("stock", "inventory", "fifo", "movement", "allocation", "audit"),
    "orders_accounting": ("order", "payment", "transaction", "receivable", "bill"),
    "queue_pending_outbox_idempotency": (
        "queue", "job", "draft", "pending", "outbox", "message_event", "idempot",
        "telegram", "notification", "delivery",
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SOURCE_EXCLUDES for part in path.relative_to(root).parts):
            continue
        if path.suffix in SOURCE_SUFFIXES and path.name not in {"ai_settings.json", "performance_metrics.json", "ai_diagnostics.json"}:
            yield path


def _config_status(env_file: Path | None, encrypted_config: Path | None) -> dict:
    names = []
    if env_file and env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                name = stripped.split("=", 1)[0].strip()
                if name:
                    names.append(name)
    secret_names = sorted({name for name in names if any(marker in name.upper() for marker in SECRET_MARKERS)})
    encrypted = {"present": bool(encrypted_config and encrypted_config.is_file())}
    if encrypted["present"]:
        encrypted.update(size=encrypted_config.stat().st_size, sha256=sha256(encrypted_config))
        try:
            value = json.loads(encrypted_config.read_text(encoding="utf-8"))
            saved = value.get("config") if isinstance(value, dict) else None
            encrypted["valid_json_object"] = isinstance(value, dict)
            encrypted["ciphertext_present"] = bool(isinstance(saved, dict) and saved.get("key_ciphertext"))
            encrypted["provider"] = saved.get("provider") if isinstance(saved, dict) else None
            encrypted["model"] = saved.get("model") if isinstance(saved, dict) else None
        except (OSError, ValueError):
            encrypted["valid_json_object"] = False
    return {
        "env_file_present": bool(env_file and env_file.is_file()),
        "configured_variable_names": sorted(set(names)),
        "secret_variable_names_present": secret_names,
        "values_included": False,
        "encrypted_ai_config": encrypted,
    }


def _connect_readonly(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def database_inventory(database: Path) -> dict:
    with _connect_readonly(database) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        counts = {name: connection.execute(f'SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34)*2)}"').fetchone()[0] for name in tables}
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
    areas = {}
    for area, markers in BUSINESS_AREAS.items():
        matched = {name: counts[name] for name in tables if any(marker in name.lower() for marker in markers)}
        areas[area] = {"tables": matched, "row_count": sum(matched.values())}
    uncategorized = {name: counts[name] for name in tables if not any(name in a["tables"] for a in areas.values())}
    return {
        "integrity_check": [row[0] for row in integrity], "user_version": user_version,
        "schema_version": schema_version, "table_counts": counts, "business_areas": areas,
        "uncategorized_tables": uncategorized,
    }


def build_inventory(source: Path, database: Path, env_file: Path | None = None,
                    encrypted_config: Path | None = None) -> dict:
    files = {str(path.relative_to(source)): {"sha256": sha256(path), "size": path.stat().st_size}
             for path in _source_files(source)}
    requirement_files = {name: sha256(source / name) for name in ("requirements.txt", "requirements-aliyun-address.txt", "pyproject.toml") if (source / name).is_file()}
    packages = {dist.metadata.get("Name", "unknown"): dist.version for dist in importlib.metadata.distributions()}
    return {
        "format": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source.resolve()), "database_source": str(database.resolve()),
        "source_files": files,
        "dependencies": {"python": platform.python_version(), "requirement_file_sha256": requirement_files,
                         "installed_packages": dict(sorted(packages.items(), key=lambda item: item[0].lower()))},
        "configuration": _config_status(env_file, encrypted_config),
        "database": database_inventory(database),
    }


def write_inventory(output: Path, **kwargs) -> dict:
    inventory = build_inventory(**kwargs)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    return inventory


def _copy_database(database: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with _connect_readonly(database) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
        destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup database integrity check failed")


def create_backup(source: Path, database: Path, destination: Path, env_file: Path | None = None,
                  encrypted_config: Path | None = None) -> dict:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite backup destination: {destination}")
    destination.mkdir(parents=True, mode=0o700)
    (destination / "source").mkdir(mode=0o700)
    inventory = build_inventory(source, database, env_file, encrypted_config)
    for relative in inventory["source_files"]:
        target = destination / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    if env_file and env_file.is_file():
        secure = destination / "secure"
        secure.mkdir(mode=0o700)
        shutil.copy2(env_file, secure / "runtime.env")
        os.chmod(secure / "runtime.env", 0o600)
    if encrypted_config and encrypted_config.is_file():
        secure = destination / "secure"
        secure.mkdir(mode=0o700, exist_ok=True)
        shutil.copy2(encrypted_config, secure / "ai_settings.json")
        os.chmod(secure / "ai_settings.json", 0o600)
    _copy_database(database, destination / "database" / "orders.sqlite3")
    inventory["backup_files"] = {
        "database/orders.sqlite3": sha256(destination / "database" / "orders.sqlite3"),
        **({"secure/runtime.env": sha256(destination / "secure" / "runtime.env")} if env_file and env_file.is_file() else {}),
        **({"secure/ai_settings.json": sha256(destination / "secure" / "ai_settings.json")} if encrypted_config and encrypted_config.is_file() else {}),
    }
    (destination / "manifest.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    return inventory


def restore_backup(backup: Path, destination: Path) -> dict:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite restore destination: {destination}")
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    for relative, expected in manifest.get("backup_files", {}).items():
        if sha256(backup / relative) != expected:
            raise RuntimeError(f"backup hash mismatch: {relative}")
    for relative, fact in manifest["source_files"].items():
        if sha256(backup / "source" / relative) != fact["sha256"]:
            raise RuntimeError(f"source hash mismatch: {relative}")
    shutil.copytree(backup, destination)
    restored_db = destination / "database" / "orders.sqlite3"
    restored = database_inventory(restored_db)
    expected = manifest["database"]
    if restored["integrity_check"] != ["ok"] or restored["table_counts"] != expected["table_counts"]:
        raise RuntimeError("restored database validation failed")
    receipt = {
        "verified_at": datetime.now(timezone.utc).isoformat(), "integrity_check": "ok",
        "source_hashes_verified": len(manifest["source_files"]),
        "database_tables_verified": len(restored["table_counts"]),
        "database_rows_verified": sum(restored["table_counts"].values()),
        "business_areas": restored["business_areas"],
        "restored_database": str(restored_db.resolve()),
    }
    (destination / "restore-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inventory", "backup"):
        item = commands.add_parser(name)
        item.add_argument("--source", type=Path, required=True)
        item.add_argument("--database", type=Path, required=True)
        item.add_argument("--env-file", type=Path)
        item.add_argument("--encrypted-config", type=Path)
        item.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _arguments(argv)
    if args.command == "inventory":
        result = write_inventory(args.output, source=args.source, database=args.database,
                                 env_file=args.env_file, encrypted_config=args.encrypted_config)
    elif args.command == "backup":
        result = create_backup(args.source, args.database, args.output, args.env_file, args.encrypted_config)
    else:
        result = restore_backup(args.backup, args.output)
    print(json.dumps({"status": "ok", "command": args.command, "summary": {
        "source_files": len(result.get("source_files", {})),
        "database_tables": len((result.get("database") or {}).get("table_counts", {})),
        "database_rows_verified": result.get("database_rows_verified"),
    }}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
