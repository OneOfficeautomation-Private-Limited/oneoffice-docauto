"""Copy a SQLite DocFlow install into PostgreSQL (DocFlow 4 stores everything in PostgreSQL).

    python -m docauto.migrate_sqlite <data dir> [--database-url URL] [--if-needed] [--dry-run]

Copies every tenant: <data dir>/docauto.db (the default tenant) and <data dir>/tenants/<id>/docauto.db, each into
its own schema (docflow_<tenant>) of DOCAUTO_DATABASE_URL, or of the embedded server in <data dir>/postgres when
that is not set (the same database the service then uses). Files stay where they are (documents keep their paths)
and the SQLite files are left untouched.

Idempotent: each tenant is copied in one transaction that also records it (meta "sqlite_migrated:<file>"); a second
run skips tenants already copied. Rows whose key is already in PostgreSQL are kept as they are there. With
--if-needed the whole run is skipped once <data dir>/docauto.db.migrated exists (windows\\start.ps1 uses this).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import db, tenants
from .store import LEGACY_TABLES, Store

log = logging.getLogger("docauto.migrate")
# tables with a serial id: copied once (new ids), guarded by the meta marker; the others keep their keys
SERIAL = {"corrections", "df_deliveries", "usage"}
NO_KEY = {"df_dedupe"}
KEYS = {"jobs": ["id"], "documents": ["id"], "schemas": ["id"], "instances": ["id"], "df_objects": ["kind", "id"],
        "df_reftables": ["id"]}
MARKER = "docauto.db.migrated"


def _sources(data_dir: Path) -> list[tuple[str, Path]]:
    out = []
    if (data_dir / "docauto.db").exists():
        out.append((tenants.DEFAULT, data_dir / "docauto.db"))
    for t in tenants.known(data_dir):
        f = data_dir / "tenants" / t / "docauto.db"
        if f.exists():
            out.append((t, f))
    return out


def _pg_columns(store: Store, table: str) -> list[str]:
    rows = store._all("SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=? "
                      "ORDER BY ordinal_position", (store.schema, table))
    return [r["column_name"] for r in rows]


def _copy_table(src: sqlite3.Connection, store: Store, table: str, first_time: bool) -> int:
    if not src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return 0
    if (table in SERIAL or table in NO_KEY) and not first_time:
        return 0
    have = [r[1] for r in src.execute(f"PRAGMA table_info({table})").fetchall()]
    cols = [c for c in _pg_columns(store, table) if c in have and not (table in SERIAL and c == "id")]
    order = " ORDER BY id" if table in SERIAL else ""
    n = 0
    sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
           + (f" ON CONFLICT ({','.join(KEYS[table])}) DO NOTHING" if table in KEYS else ""))
    for row in src.execute(f"SELECT {','.join(cols)} FROM {table}{order}"):
        n += store._exec(sql, tuple(row))
    return n


def migrate_tenant(tenant: str, sqlite_file: Path, data_dir: Path, url: str, schema_prefix: Optional[str] = None,
                   dry_run: bool = False) -> dict:
    key = "sqlite_migrated:" + hashlib.sha1(str(sqlite_file.resolve()).lower().encode()).hexdigest()[:16]
    src = sqlite3.connect(f"file:{sqlite_file}?mode=ro", uri=True)
    try:
        counts = {t: src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in LEGACY_TABLES
                  if src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()}
        if dry_run:
            return {"tenant": tenant, "file": str(sqlite_file), "rows": counts, "copied": None}
        store = Store(str(tenants.data_dir(data_dir, tenant)), database_url=url, tenant=tenant,
                      schema_prefix=schema_prefix, key_file=str(data_dir / "file.key"))
        done = store.meta_get(key)
        copied = {}
        with store.tx():   # all of a tenant, or nothing
            for t in LEGACY_TABLES:
                copied[t] = _copy_table(src, store, t, first_time=done is None)
            for t in SERIAL:   # serial ids continue after the copied rows
                store._exec(f"SELECT setval(pg_get_serial_sequence('{t}','id'), COALESCE((SELECT MAX(id) FROM {t}),0)+1, false)")
            if done is None:
                store.meta_set(key, json.dumps({"file": str(sqlite_file), "at": datetime.now(timezone.utc).isoformat(),
                                                "rows": counts}))
        return {"tenant": tenant, "schema": store.schema, "file": str(sqlite_file), "rows": counts, "copied": copied,
                "alreadyMigrated": done is not None}
    finally:
        src.close()


def migrate(data_dir: str | Path, database_url: Optional[str] = None, schema_prefix: Optional[str] = None,
            dry_run: bool = False) -> list[dict]:
    data_dir = Path(data_dir).resolve()
    if not data_dir.is_dir():
        raise SystemExit(f"data folder not found: {data_dir}")
    sources = _sources(data_dir)
    if not sources:
        return []
    url = db.resolve_url(database_url, data_dir)
    return [migrate_tenant(t, f, data_dir, url, schema_prefix, dry_run) for t, f in sources]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m docauto.migrate_sqlite", description=__doc__.splitlines()[0])
    ap.add_argument("data_dir", help="DocFlow data folder (holds docauto.db and tenants/)")
    ap.add_argument("--database-url", help="PostgreSQL URL (default: DOCAUTO_DATABASE_URL, else the embedded server)")
    ap.add_argument("--if-needed", action="store_true", help=f"do nothing when <data dir>/{MARKER} exists")
    ap.add_argument("--dry-run", action="store_true", help="only count what would be copied")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    marker = Path(a.data_dir) / MARKER
    if a.if_needed and (marker.exists() or not _sources(Path(a.data_dir))):
        return 0
    results = migrate(a.data_dir, a.database_url, dry_run=a.dry_run)
    if not results:
        print("No SQLite database found: nothing to migrate.")
        return 0
    for r in results:
        state = ("would copy" if a.dry_run else "already migrated, checked" if r.get("alreadyMigrated") else "copied")
        rows = ", ".join(f"{t} {n}" for t, n in r["rows"].items() if n)
        print(f"{r['tenant']}: {state} ({rows or 'empty'})" + (f" -> schema {r['schema']}" if r.get("schema") else ""))
    if not a.dry_run:
        marker.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "tenants": results},
                                     indent=2, default=str), encoding="utf-8")
        print(f"Done. The SQLite files were not changed; {marker.name} records the migration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
