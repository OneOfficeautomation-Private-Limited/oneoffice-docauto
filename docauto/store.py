"""Persistence: jobs, documents/results, schemas, usage metering, outbox, test sets (PostgreSQL, see docauto.db).

Each tenant has its own schema; files stay under the tenant's data folder, encrypted (docauto.filecrypt).
Queries use "?" placeholders (translated for psycopg). Outside a transaction every statement commits on its own;
inside `with store.tx():` everything the thread does on this store shares one transaction (nested = savepoint).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import psycopg
from psycopg import sql

from . import db, filecrypt
from .models import DocumentResult, FieldSchema

log = logging.getLogger("docauto.store")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, document_id TEXT, status TEXT, request TEXT, error TEXT,
  created_at TEXT, updated_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, worker TEXT, heartbeat_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_document ON jobs(document_id);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, file_name TEXT, mime_type TEXT, sha256 TEXT, path TEXT,
  result TEXT, schema_id TEXT, review_status TEXT, created_at TEXT,
  instance_id TEXT, instance_version INTEGER, status TEXT, source TEXT, processing_ms INTEGER, updated_at TEXT,
  validated_by TEXT, field_count INTEGER, corrected_count INTEGER, doctype_id TEXT, parent_id TEXT, flags TEXT,
  sla_notified INTEGER);
CREATE INDEX IF NOT EXISTS ix_doc_instance ON documents(instance_id, status);
CREATE INDEX IF NOT EXISTS ix_doc_created ON documents(created_at);
CREATE INDEX IF NOT EXISTS ix_doc_parent ON documents(parent_id);
CREATE TABLE IF NOT EXISTS schemas (id TEXT PRIMARY KEY, name TEXT, body TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS instances (
  id TEXT PRIMARY KEY, name TEXT, body TEXT, status TEXT, version INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS corrections (
  id BIGSERIAL PRIMARY KEY, instance_id TEXT, document_id TEXT, field TEXT,
  original TEXT, corrected TEXT, reviewer TEXT, ts TEXT);
CREATE INDEX IF NOT EXISTS ix_corrections_instance ON corrections(instance_id);
CREATE TABLE IF NOT EXISTS df_objects (
  kind TEXT, id TEXT, name TEXT, body TEXT, status TEXT, updated_at TEXT, PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS df_reftables (id TEXT PRIMARY KEY, name TEXT, columns TEXT, rows TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS df_dedupe (flow_id TEXT, key TEXT, document_id TEXT, ts TEXT);
CREATE INDEX IF NOT EXISTS ix_dedupe ON df_dedupe(flow_id, key);
CREATE TABLE IF NOT EXISTS df_deliveries (
  id BIGSERIAL PRIMARY KEY, flow_id TEXT, document_id TEXT, delivery TEXT, type TEXT,
  ok INTEGER, detail TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS usage (
  id BIGSERIAL PRIMARY KEY, tenant_id TEXT, job_id TEXT, document_id TEXT,
  provider_id TEXT, option_pack TEXT, capability TEXT, pages INTEGER, credits DOUBLE PRECISION,
  runs_locally INTEGER, regions TEXT, ts TEXT, model TEXT);
CREATE INDEX IF NOT EXISTS ix_usage_document ON usage(document_id);
CREATE TABLE IF NOT EXISTS outbox (
  id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL, document_id TEXT, flow_id TEXT, name TEXT, payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  next_at TIMESTAMPTZ NOT NULL DEFAULT now(), locked_until TIMESTAMPTZ, claim TEXT, last_error TEXT,
  created_at TEXT, sent_at TEXT);
CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox(status, next_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS testsets (
  id TEXT PRIMARY KEY, name TEXT, doctype_id TEXT, description TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS testset_items (
  id TEXT PRIMARY KEY, set_id TEXT NOT NULL, file_name TEXT, mime_type TEXT, path TEXT, expected TEXT, tags TEXT,
  source TEXT, source_document_id TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_testset_items ON testset_items(set_id);
CREATE TABLE IF NOT EXISTS testset_runs (
  id TEXT PRIMARY KEY, set_id TEXT NOT NULL, status TEXT, combos TEXT, results TEXT, rows TEXT, error TEXT,
  worker TEXT, created_at TEXT, finished_at TEXT);
CREATE INDEX IF NOT EXISTS ix_testset_runs ON testset_runs(set_id, created_at);
"""
# tables a SQLite install had (docauto.migrate_sqlite copies them)
LEGACY_TABLES = ("jobs", "documents", "schemas", "instances", "corrections", "df_objects", "df_reftables",
                 "df_dedupe", "df_deliveries", "usage")
HOST = socket.gethostname()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@lru_cache(maxsize=1024)
def _pg(q: str) -> str:
    """SQLite-style ? placeholders -> psycopg %s (no query here has a literal ? or %)."""
    return q.replace("%", "%%").replace("?", "%s")


def _clean(v: Any) -> Any:
    """PostgreSQL text cannot hold NUL characters."""
    return v.replace("\x00", "") if isinstance(v, str) else v


def worker_id() -> str:
    return f"{HOST}:{os.getpid()}"


def pid_alive(worker: Optional[str]) -> bool:
    """Whether the process behind a worker id (host:pid[:...]) still runs. Other hosts: assumed alive."""
    parts = (worker or "").split(":")
    if len(parts) < 2 or parts[0] != HOST:
        return True
    try:
        import psutil
        return psutil.pid_exists(int(parts[1]))
    except (ImportError, ValueError):
        return True


class Store:
    def __init__(self, data_dir: str, database_url: Optional[str] = None, schema: Optional[str] = None,
                 tenant: str = "default", schema_prefix: Optional[str] = None, key_file: Optional[str] = None):
        self.dir = Path(data_dir)
        (self.dir / "files").mkdir(parents=True, exist_ok=True)
        self.tenant = tenant
        self.database_url = db.resolve_url(database_url, data_dir)
        self.schema = schema or db.schema_for(tenant, schema_prefix)
        self.pool = db.pool(self.database_url)
        self.cipher = filecrypt.cipher(key_file or self.dir / "file.key")
        self._local = threading.local()
        self._create()

    def _create(self) -> None:
        """Schema and tables (idempotent; an advisory lock keeps concurrent first starts apart)."""
        with self.pool.connection(self.schema) as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.schema,))
                conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
                conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.schema)))
                conn.execute(SCHEMA_SQL)

    # ---------------------------------------------------------------- statements and transactions
    @contextmanager
    def tx(self) -> Iterator[None]:
        """One transaction for everything this thread does on this store inside the block (nested: a savepoint).
        Callbacks registered with after_commit run once the outermost block has committed."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with conn.transaction():
                yield
            return
        with self.pool.connection(self.schema) as conn:
            self._local.conn, self._local.after = conn, []
            try:
                with conn.transaction():
                    yield
            except BaseException:
                self._local.conn, self._local.after = None, []
                raise
            after, self._local.conn, self._local.after = self._local.after, None, []
        for fn in after:
            try:
                fn()
            except Exception:
                log.exception("after-commit callback failed")

    def after_commit(self, fn: Callable[[], None]) -> None:
        if getattr(self._local, "conn", None) is not None:
            self._local.after.append(fn)
        else:
            fn()

    def _run(self, q: str, args: tuple, fetch: Callable):
        args = tuple(_clean(a) for a in args)
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return fetch(conn.execute(_pg(q), args))
        for attempt in (1, 2):
            try:
                with self.pool.connection(self.schema) as conn:
                    return fetch(conn.execute(_pg(q), args))
            except psycopg.OperationalError:
                if attempt == 2:  # a pooled connection the server dropped: one retry on a fresh one
                    raise

    def _exec(self, q: str, args: tuple = ()) -> int:
        return self._run(q, args, lambda c: c.rowcount)

    def _one(self, q: str, args: tuple = ()) -> Optional[dict]:
        return self._run(q, args, lambda c: c.fetchone())

    def _all(self, q: str, args: tuple = ()) -> list[dict]:
        return self._run(q, args, lambda c: c.fetchall())

    # ---------------------------------------------------------------- files (encrypted at rest)
    def write_file(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.cipher.write(path, data)

    def read_file(self, path: Path | str) -> bytes:
        return self.cipher.read(path)

    # documents ---------------------------------------------------------------
    def save_file(self, data: bytes, file_name: str, mime: str, sha: str) -> str:
        doc_id = uuid.uuid4().hex
        path = self.dir / "files" / f"{doc_id}_{Path(file_name).name}"
        self.write_file(path, data)
        self._exec("INSERT INTO documents (id,file_name,mime_type,sha256,path,created_at) VALUES (?,?,?,?,?,?)",
                   (doc_id, file_name, mime, sha, str(path), now()))
        return doc_id

    def delete_document(self, doc_id: str) -> None:
        """Forgets a document and its jobs and file (used by the Test lab: nothing it reads is kept). Usage stays."""
        r = self._one("SELECT path FROM documents WHERE id=?", (doc_id,))
        with self.tx():
            self._exec("DELETE FROM jobs WHERE document_id=?", (doc_id,))
            self._exec("DELETE FROM documents WHERE id=?", (doc_id,))
        if r and r["path"]:
            Path(r["path"]).unlink(missing_ok=True)

    def file_bytes(self, doc_id: str) -> Optional[tuple[bytes, str, str]]:
        r = self._one("SELECT path,file_name,mime_type FROM documents WHERE id=?", (doc_id,))
        if not r:
            return None
        return self.read_file(r["path"]), r["file_name"], r["mime_type"]

    def save_result(self, result: DocumentResult, schema_id: Optional[str] = None) -> None:
        self._exec("UPDATE documents SET result=?, schema_id=COALESCE(?,schema_id), review_status=? WHERE id=?",
                   (result.model_dump_json(), schema_id, result.review.status.value, result.documentId))

    def get_result(self, doc_id: str) -> Optional[DocumentResult]:
        r = self._one("SELECT result FROM documents WHERE id=?", (doc_id,))
        if not r or not r["result"]:
            return None
        return DocumentResult.model_validate_json(r["result"])

    def document_schema_id(self, doc_id: str) -> Optional[str]:
        r = self._one("SELECT schema_id FROM documents WHERE id=?", (doc_id,))
        return r["schema_id"] if r else None

    def list_documents(self, limit: int = 50) -> list[dict]:
        rows = self._all("SELECT d.id, d.file_name, d.created_at, d.review_status, d.schema_id, j.status AS job_status "
                         "FROM documents d LEFT JOIN jobs j ON j.document_id = d.id "
                         "ORDER BY d.created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # document lifecycle -----------------------------------------------------
    DOC_STATUSES = ("queued", "processing", "success", "validation", "validated", "invalid", "failed", "split")

    def set_doc_meta(self, doc_id: str, instance_id: Optional[str], version: Optional[int], source: str,
                     doctype_id: Optional[str] = None, parent_id: Optional[str] = None) -> None:
        self._exec("UPDATE documents SET instance_id=?, instance_version=?, source=?, status='queued', updated_at=?, "
                   "doctype_id=?, parent_id=? WHERE id=?",
                   (instance_id, version, source, now(), doctype_id, parent_id, doc_id))

    def set_doc_fields(self, doc_id: str, **cols) -> None:
        allowed = {"doctype_id", "flags", "sla_notified", "parent_id", "instance_id"}
        sets = {k: v for k, v in cols.items() if k in allowed}
        if sets:
            self._exec(f"UPDATE documents SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE id=?",
                       (*sets.values(), now(), doc_id))

    # ---- DocFlow objects (doc types, flows) -----------------------------------
    def df_save(self, kind: str, obj_id: str, name: str, body: str, status: str = "") -> None:
        self._exec("INSERT INTO df_objects VALUES (?,?,?,?,?,?) ON CONFLICT (kind, id) DO UPDATE SET "
                   "name=EXCLUDED.name, body=EXCLUDED.body, status=EXCLUDED.status, updated_at=EXCLUDED.updated_at",
                   (kind, obj_id, name, body, status, now()))

    def df_get(self, kind: str, obj_id: str) -> Optional[str]:
        r = self._one("SELECT body FROM df_objects WHERE kind=? AND id=?", (kind, obj_id))
        return r["body"] if r else None

    def df_list(self, kind: str) -> list[str]:
        return [r["body"] for r in self._all("SELECT body FROM df_objects WHERE kind=? ORDER BY name", (kind,))]

    def df_delete(self, kind: str, obj_id: str) -> None:
        self._exec("DELETE FROM df_objects WHERE kind=? AND id=?", (kind, obj_id))

    def ref_save(self, ref_id: str, name: str, columns: list[str], rows: list[dict]) -> None:
        self._exec("INSERT INTO df_reftables VALUES (?,?,?,?,?) ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, "
                   "columns=EXCLUDED.columns, rows=EXCLUDED.rows, updated_at=EXCLUDED.updated_at",
                   (ref_id, name, json.dumps(columns, ensure_ascii=False), json.dumps(rows, ensure_ascii=False), now()))

    def ref_get(self, ref_id: str) -> Optional[dict]:
        r = self._one("SELECT * FROM df_reftables WHERE id=?", (ref_id,))
        if not r:
            return None
        return {"id": r["id"], "name": r["name"], "columns": json.loads(r["columns"]), "rows": json.loads(r["rows"]),
                "updatedAt": r["updated_at"]}

    def ref_list(self) -> list[dict]:
        return [{"id": r["id"], "name": r["name"], "columns": json.loads(r["columns"]),
                 "rowCount": len(json.loads(r["rows"])), "updatedAt": r["updated_at"]}
                for r in self._all("SELECT * FROM df_reftables ORDER BY name")]

    def ref_delete(self, ref_id: str) -> None:
        self._exec("DELETE FROM df_reftables WHERE id=?", (ref_id,))

    def dedupe_find(self, flow_id: str, key: str, exclude: str) -> Optional[str]:
        r = self._one("SELECT k.document_id FROM df_dedupe k JOIN documents d ON d.id=k.document_id "
                      "WHERE k.flow_id=? AND k.key=? AND k.document_id<>? AND COALESCE(d.status,'')<>'invalid' "
                      "ORDER BY k.ts LIMIT 1", (flow_id, key, exclude))
        return r["document_id"] if r else None

    def dedupe_add(self, flow_id: str, key: str, doc_id: str) -> None:
        with self.tx():
            self._exec("DELETE FROM df_dedupe WHERE flow_id=? AND document_id=?", (flow_id, doc_id))
            self._exec("INSERT INTO df_dedupe VALUES (?,?,?,?)", (flow_id, key, doc_id, now()))

    def delivery_log(self, flow_id: str, doc_id: str, name: str, typ: str, ok: bool, detail: str) -> None:
        self._exec("INSERT INTO df_deliveries (flow_id,document_id,delivery,type,ok,detail,ts) VALUES (?,?,?,?,?,?,?)",
                   (flow_id, doc_id, name, typ, int(ok), detail[:500], now()))

    def delivery_list(self, flow_id: Optional[str], limit: int = 100) -> list[dict]:
        q = "SELECT * FROM df_deliveries" + (" WHERE flow_id=?" if flow_id else "") + " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self._all(q, ((flow_id, limit) if flow_id else (limit,)))]

    def children(self, parent_id: str) -> list[dict]:
        return [dict(r) for r in self._all("SELECT id,file_name,status,doctype_id FROM documents WHERE parent_id=? "
                                           "ORDER BY created_at", (parent_id,))]

    def overdue_reviews(self, flow_id: str, older_than_iso: str) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT id,file_name,created_at FROM documents WHERE instance_id=? AND status='validation' "
            "AND created_at<? AND COALESCE(sla_notified,0)=0", (flow_id, older_than_iso))]

    def set_doc_status(self, doc_id: str, status: str, processing_ms: Optional[int] = None,
                       validated_by: Optional[str] = None, field_count: Optional[int] = None,
                       corrected_count: Optional[int] = None) -> None:
        self._exec("UPDATE documents SET status=?, processing_ms=COALESCE(?::integer,processing_ms), "
                   "validated_by=COALESCE(?,validated_by), field_count=COALESCE(?::integer,field_count), "
                   "corrected_count=COALESCE(?::integer,corrected_count), updated_at=? WHERE id=?",
                   (status, processing_ms, validated_by, field_count, corrected_count, now(), doc_id))

    def doc_row(self, doc_id: str) -> Optional[dict]:
        r = self._one("SELECT id,file_name,mime_type,created_at,updated_at,status,instance_id,instance_version,source,"
                      "processing_ms,validated_by,field_count,corrected_count,review_status,doctype_id,parent_id,flags "
                      "FROM documents WHERE id=?",
                      (doc_id,))
        return dict(r) if r else None

    def last_job_error(self, doc_id: str) -> Optional[dict]:
        job = self._one("SELECT error FROM jobs WHERE document_id=? ORDER BY created_at DESC LIMIT 1", (doc_id,))
        return json.loads(job["error"]) if job and job["error"] else None

    def instance_documents(self, instance_id: Optional[str], statuses: Optional[list[str]] = None,
                           limit: int = 200, offset: int = 0) -> list[dict]:
        q = ("SELECT id,file_name,created_at,updated_at,status,instance_id,instance_version,source,processing_ms,"
             "validated_by,field_count,corrected_count,doctype_id,parent_id,flags FROM documents "
             "WHERE instance_id IS NOT NULL")
        args: list = []
        if instance_id:
            q += " AND instance_id=?"
            args.append(instance_id)
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        return [dict(r) for r in self._all(q, tuple(args + [limit, offset]))]

    def next_for_validation(self, instance_id: Optional[str], after: Optional[str] = None) -> Optional[str]:
        q = "SELECT id FROM documents WHERE status='validation'"
        args: list = []
        if instance_id:
            q += " AND instance_id=?"
            args.append(instance_id)
        if after:
            q += " AND id<>?"
            args.append(after)
        r = self._one(q + " ORDER BY created_at LIMIT 1", tuple(args))
        return r["id"] if r else None

    def instance_stats(self, instance_id: Optional[str]) -> dict:
        where, args = (("WHERE instance_id=? AND COALESCE(status,'')<>'split'", (instance_id,)) if instance_id
                       else ("WHERE instance_id IS NOT NULL AND COALESCE(status,'')<>'split'", ()))
        by = {r["status"]: r["n"] for r in self._all(f"SELECT status, COUNT(*) n FROM documents {where} GROUP BY status", args)}
        agg = self._one(f"SELECT AVG(processing_ms) ms, SUM(COALESCE(field_count,0)) f, "
                        f"SUM(COALESCE(corrected_count,0)) c FROM documents {where} AND status IN ('success','validated')",
                        args)
        daily = [dict(r) for r in self._all(
            f"SELECT substr(created_at,1,10) AS day, COUNT(*) total, "
            f"SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) stp, "
            f"SUM(CASE WHEN status IN ('validation','validated','invalid') THEN 1 ELSE 0 END) human, "
            f"SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed "
            f"FROM documents {where} GROUP BY day ORDER BY day DESC LIMIT 14", args)][::-1]
        pages = self._one(f"SELECT COALESCE(SUM(u.pages),0) p, COALESCE(SUM(u.credits),0) cr FROM usage u "
                          f"JOIN documents d ON d.id=u.document_id {where.replace(" AND COALESCE(status,'')<>'split'", '').replace('instance_id', 'd.instance_id')} "
                          f"AND u.capability IN ('ocr_printed','tables','ocr_handwriting','layout','key_value')", args)
        month = self._one(f"SELECT COUNT(*) n FROM documents {where} AND created_at >= ?",
                          (*args, datetime.now(timezone.utc).strftime("%Y-%m-01")))
        done = sum(by.get(k, 0) for k in ("success", "validation", "validated", "invalid"))
        fields, corrected = (agg["f"] or 0), (agg["c"] or 0)
        return {"byStatus": {k: by.get(k, 0) for k in self.DOC_STATUSES}, "total": sum(by.values()),
                "processed": done, "stpRate": round(by.get("success", 0) / done, 4) if done else None,
                "fieldAccuracy": round(1 - corrected / fields, 4) if fields else None,
                "avgProcessingMs": int(agg["ms"]) if agg["ms"] else None, "pages": pages["p"],
                "credits": pages["cr"], "daily": daily, "thisMonth": month["n"]}

    def month_usage(self, since_iso: str) -> dict:
        """Pages read and credits of documents in processes since a date (the overview's "this month")."""
        return self._one("SELECT COALESCE(SUM(u.pages),0) p, COALESCE(SUM(u.credits),0) cr FROM usage u "
                         "JOIN documents d ON d.id=u.document_id WHERE d.instance_id IS NOT NULL AND u.ts >= ? "
                         "AND u.capability IN ('ocr_printed','tables','ocr_handwriting','layout','key_value')",
                         (since_iso,))

    # corrections ------------------------------------------------------------
    def add_correction(self, instance_id: Optional[str], doc_id: str, field: str, original: Any, corrected: Any,
                       reviewer: str) -> None:
        self._exec("INSERT INTO corrections (instance_id,document_id,field,original,corrected,reviewer,ts) "
                   "VALUES (?,?,?,?,?,?,?)", (instance_id, doc_id, field, json.dumps(original, ensure_ascii=False),
                                              json.dumps(corrected, ensure_ascii=False), reviewer, now()))

    def corrections(self, instance_id: str, limit: int = 50) -> list[dict]:
        rows = self._all("SELECT document_id,field,original,corrected,reviewer,ts FROM corrections WHERE instance_id=? "
                         "ORDER BY id DESC LIMIT ?", (instance_id, limit))
        out = []
        for r in rows:
            d = dict(r)
            d["original"], d["corrected"] = json.loads(d["original"]), json.loads(d["corrected"])
            out.append(d)
        return out

    # learning instances -----------------------------------------------------
    def save_instance(self, inst_id: str, name: str, body: str, status: str, version: int, created_at: str) -> None:
        self._exec("INSERT INTO instances VALUES (?,?,?,?,?,?,?) ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, "
                   "body=EXCLUDED.body, status=EXCLUDED.status, version=EXCLUDED.version, "
                   "created_at=EXCLUDED.created_at, updated_at=EXCLUDED.updated_at",
                   (inst_id, name, body, status, version, created_at, now()))

    def get_instance_body(self, inst_id: str) -> Optional[str]:
        r = self._one("SELECT body FROM instances WHERE id=?", (inst_id,))
        return r["body"] if r else None

    def list_instance_bodies(self) -> list[str]:
        return [r["body"] for r in self._all("SELECT body FROM instances ORDER BY name")]

    def delete_instance(self, inst_id: str) -> None:
        self._exec("DELETE FROM instances WHERE id=?", (inst_id,))

    def reviews(self, status: str) -> list[dict]:
        rows = self._all("SELECT id,file_name,created_at,review_status FROM documents WHERE review_status=? "
                         "ORDER BY created_at", (status,))
        return [dict(r) for r in rows]

    # jobs (durable: workers claim them with FOR UPDATE SKIP LOCKED) ------------
    def create_job(self, document_id: str, request: dict, worker: Optional[str] = None) -> str:
        """A queued job; with worker, a job already claimed by that worker (processed in the caller's thread)."""
        job_id = uuid.uuid4().hex
        if worker:
            self._exec("INSERT INTO jobs (id,document_id,status,request,error,created_at,updated_at,attempts,worker,"
                       "heartbeat_at) VALUES (?,?,'running',?,NULL,?,?,1,?,now())",
                       (job_id, document_id, json.dumps(request), now(), now(), worker))
        else:
            self._exec("INSERT INTO jobs (id,document_id,status,request,error,created_at,updated_at) "
                       "VALUES (?,?,'queued',?,NULL,?,?)", (job_id, document_id, json.dumps(request), now(), now()))
        return job_id

    def claim_jobs(self, worker: str, limit: int, stale_seconds: float) -> list[dict]:
        """Claim up to `limit` jobs: queued ones, and running ones whose worker stopped sending heartbeats."""
        rows = self._all(
            "UPDATE jobs SET status='running', worker=?, heartbeat_at=now(), attempts=attempts+1, updated_at=? "
            "WHERE id IN (SELECT id FROM jobs WHERE status='queued' OR (status='running' AND "
            "(heartbeat_at IS NULL OR heartbeat_at < now() - make_interval(secs => ?))) "
            "ORDER BY created_at LIMIT ? FOR UPDATE SKIP LOCKED) "
            "RETURNING id, document_id, request, attempts, created_at",
            (worker, now(), float(stale_seconds), int(limit)))
        out = []
        for r in sorted(rows, key=lambda r: r["created_at"] or ""):
            d = dict(r)
            d["request"] = json.loads(d["request"] or "{}")
            out.append(d)
        return out

    def heartbeat(self, job_ids: list[str], worker: str) -> None:
        if job_ids:
            self._exec(f"UPDATE jobs SET heartbeat_at=now() WHERE worker=? AND status='running' "
                       f"AND id IN ({','.join('?' * len(job_ids))})", (worker, *job_ids))

    def release_dead_jobs(self) -> int:
        """Jobs left running by a process of this host that has stopped: queued again right away."""
        dead = [r["id"] for r in self._all("SELECT id, worker FROM jobs WHERE status='running'")
                if not pid_alive(r["worker"])]
        for job_id in dead:
            self._exec("UPDATE jobs SET status='queued', worker=NULL, updated_at=? WHERE id=? AND status='running'",
                       (now(), job_id))
        return len(dead)

    def update_job(self, job_id: str, status: str, error: Optional[dict] = None) -> None:
        self._exec("UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                   (status, json.dumps(error) if error else None, now(), job_id))

    def get_job(self, job_id: str) -> Optional[dict]:
        r = self._one("SELECT id, document_id, status, request, error, created_at, updated_at FROM jobs WHERE id=?",
                      (job_id,))
        if not r:
            return None
        d = dict(r)
        d["request"] = json.loads(d["request"] or "{}")
        d["error"] = json.loads(d["error"]) if d["error"] else None
        return d

    # schemas -----------------------------------------------------------------
    def save_schema(self, schema: FieldSchema) -> FieldSchema:
        schema.id = schema.id or uuid.uuid4().hex[:12]
        self._exec("INSERT INTO schemas VALUES (?,?,?,?) ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, "
                   "body=EXCLUDED.body, updated_at=EXCLUDED.updated_at",
                   (schema.id, schema.name, schema.model_dump_json(), now()))
        return schema

    def get_schema(self, schema_id: str) -> Optional[FieldSchema]:
        r = self._one("SELECT body FROM schemas WHERE id=?", (schema_id,))
        return FieldSchema.model_validate_json(r["body"]) if r else None

    def list_schemas(self) -> list[dict]:
        return [{"id": r["id"], "name": r["name"], "updatedAt": r["updated_at"]}
                for r in self._all("SELECT id,name,updated_at FROM schemas ORDER BY name")]

    # metering ----------------------------------------------------------------
    def record_usage(self, *, tenant_id: str, job_id: str, document_id: str, provider_id: str,
                     option_pack: str, capability: str, pages: int, credits: float,
                     runs_locally: bool, regions: list[str], model: Optional[str] = None) -> None:
        self._exec("INSERT INTO usage (tenant_id,job_id,document_id,provider_id,option_pack,capability,pages,"
                   "credits,runs_locally,regions,ts,model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (tenant_id, job_id, document_id, provider_id, option_pack, capability, pages, float(credits),
                    int(runs_locally), ",".join(regions), now(), model))

    def usage_summary(self, group_by: str = "provider_id") -> list[dict[str, Any]]:
        if group_by not in ("provider_id", "option_pack", "tenant_id", "capability", "model"):
            raise ValueError("invalid group_by")
        rows = self._all(f"SELECT {group_by} AS k, COUNT(*) AS calls, SUM(pages) AS pages, "
                         f"SUM(credits) AS credits, SUM(1-runs_locally) AS outbound_calls "
                         f"FROM usage GROUP BY {group_by} ORDER BY credits DESC NULLS LAST")
        return [dict(r) for r in rows]

    # outbox: deliveries and events, sent by docauto.outbox ---------------------
    def outbox_add(self, kind: str, payload: dict, document_id: Optional[str] = None, flow_id: Optional[str] = None,
                   name: Optional[str] = None, wake: Optional[Callable[[], None]] = None) -> int:
        """Queue something to send. Inside tx() it is written in the caller's transaction (sent only if it commits)."""
        r = self._one("INSERT INTO outbox (kind,document_id,flow_id,name,payload,created_at) VALUES (?,?,?,?,?,?) "
                      "RETURNING id", (kind, document_id, flow_id, name,
                                       json.dumps(payload, ensure_ascii=False, default=str), now()))
        if wake:
            self.after_commit(wake)
        return r["id"]

    def outbox_claim(self, claim: str, limit: int = 10, lease_seconds: float = 300) -> list[dict]:
        """Due items, leased to `claim` (another sender skips them until the lease ends)."""
        rows = self._all(
            "UPDATE outbox SET claim=?, locked_until=now() + make_interval(secs => ?) WHERE id IN ("
            "SELECT id FROM outbox WHERE status='pending' AND next_at <= now() AND "
            "(locked_until IS NULL OR locked_until < now()) ORDER BY id LIMIT ? FOR UPDATE SKIP LOCKED) "
            "RETURNING id, kind, document_id, flow_id, name, payload, attempts, created_at",
            (claim, float(lease_seconds), int(limit)))
        out = []
        for r in sorted(rows, key=lambda r: r["id"]):
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def outbox_sent(self, item_id: int, claim: str) -> bool:
        """Mark sent; False when this sender no longer holds the item (it is then never marked twice)."""
        return self._exec("UPDATE outbox SET status='sent', attempts=attempts+1, sent_at=?, locked_until=NULL, "
                          "last_error=NULL WHERE id=? AND claim=? AND status='pending'", (now(), item_id, claim)) == 1

    def outbox_retry(self, item_id: int, claim: str, error: str, delay_seconds: float) -> bool:
        return self._exec("UPDATE outbox SET attempts=attempts+1, last_error=?, locked_until=NULL, "
                          "next_at=now() + make_interval(secs => ?) WHERE id=? AND claim=? AND status='pending'",
                          (error[:1000], float(delay_seconds), item_id, claim)) == 1

    def outbox_failed(self, item_id: int, claim: str, error: str) -> bool:
        return self._exec("UPDATE outbox SET status='failed', attempts=attempts+1, last_error=?, locked_until=NULL "
                          "WHERE id=? AND claim=? AND status='pending'", (error[:1000], item_id, claim)) == 1

    def outbox_list(self, status: Optional[str] = None, limit: int = 100) -> list[dict]:
        q = ("SELECT id, kind, document_id, flow_id, name, status, attempts, next_at, last_error, created_at, sent_at "
             "FROM outbox" + (" WHERE status=?" if status else "") + " ORDER BY id DESC LIMIT ?")
        rows = [dict(r) for r in self._all(q, ((status, limit) if status else (limit,)))]
        for r in rows:
            r["next_at"] = r["next_at"].isoformat() if r["next_at"] else None
        return rows

    # meta (small key/value state: last retention run...) -----------------------
    def meta_get(self, key: str) -> Optional[str]:
        r = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else None

    def meta_set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO meta VALUES (?,?,?) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, "
                   "updated_at=EXCLUDED.updated_at", (key, value, now()))

    # retention ---------------------------------------------------------------
    KEEP_STATUSES = ("queued", "processing", "validation")   # still in work: kept until they are done

    def purge_documents(self, older_than_iso: str, batch: int = 500) -> int:
        """Delete documents created before the cutoff (rows, results, jobs, dedupe keys, corrections, outbox items,
        files), except documents still in work. Usage rows (billing) stay. Test-set files are not documents."""
        total = 0
        while True:
            rows = self._all(f"SELECT id, path FROM documents WHERE created_at < ? AND "
                             f"COALESCE(status,'') NOT IN ({','.join('?' * len(self.KEEP_STATUSES))}) "
                             f"AND COALESCE(source,'') <> 'testset' LIMIT ?",
                             (older_than_iso, *self.KEEP_STATUSES, batch))
            if not rows:
                break
            ids = [r["id"] for r in rows]
            marks = ",".join("?" * len(ids))
            with self.tx():
                for table in ("jobs", "df_dedupe", "corrections", "outbox"):
                    self._exec(f"DELETE FROM {table} WHERE document_id IN ({marks})", tuple(ids))
                self._exec(f"DELETE FROM documents WHERE id IN ({marks})", tuple(ids))
            for r in rows:
                if r["path"]:
                    try:
                        Path(r["path"]).unlink(missing_ok=True)
                    except OSError as e:
                        log.warning("could not delete %s: %s", r["path"], e)
            total += len(rows)
        self._exec("DELETE FROM outbox WHERE status<>'pending' AND created_at < ?", (older_than_iso,))
        return total

    # test sets (quality gate, docauto.testsets) --------------------------------
    def testset_save(self, set_id: str, name: str, doctype_id: str, description: str) -> None:
        self._exec("INSERT INTO testsets VALUES (?,?,?,?,?,?) ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, "
                   "description=EXCLUDED.description, updated_at=EXCLUDED.updated_at",
                   (set_id, name, doctype_id, description, now(), now()))

    def testset_get(self, set_id: str) -> Optional[dict]:
        r = self._one("SELECT s.*, (SELECT COUNT(*) FROM testset_items i WHERE i.set_id=s.id) AS item_count "
                      "FROM testsets s WHERE id=?", (set_id,))
        return dict(r) if r else None

    def testset_list(self, doctype_id: Optional[str] = None) -> list[dict]:
        q = ("SELECT s.*, (SELECT COUNT(*) FROM testset_items i WHERE i.set_id=s.id) AS item_count FROM testsets s"
             + (" WHERE doctype_id=?" if doctype_id else "") + " ORDER BY name")
        return [dict(r) for r in self._all(q, (doctype_id,) if doctype_id else ())]

    def testset_delete(self, set_id: str) -> list[str]:
        paths = [r["path"] for r in self._all("SELECT path FROM testset_items WHERE set_id=?", (set_id,))]
        with self.tx():
            for table in ("testset_items", "testset_runs"):
                self._exec(f"DELETE FROM {table} WHERE set_id=?", (set_id,))
            self._exec("DELETE FROM testsets WHERE id=?", (set_id,))
        return paths

    def testset_add_item(self, item: dict) -> None:
        self._exec("INSERT INTO testset_items (id,set_id,file_name,mime_type,path,expected,tags,source,"
                   "source_document_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (item["id"], item["set_id"], item["file_name"], item["mime_type"], item["path"],
                    json.dumps(item["expected"], ensure_ascii=False), json.dumps(item.get("tags") or []),
                    item.get("source"), item.get("source_document_id"), now()))
        self._exec("UPDATE testsets SET updated_at=? WHERE id=?", (now(), item["set_id"]))

    def testset_items(self, set_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM testset_items WHERE set_id=? ORDER BY created_at, id", (set_id,))
        return [{**dict(r), "expected": json.loads(r["expected"] or "{}"), "tags": json.loads(r["tags"] or "[]")}
                for r in rows]

    def testset_item(self, set_id: str, item_id: str) -> Optional[dict]:
        r = self._one("SELECT * FROM testset_items WHERE set_id=? AND id=?", (set_id, item_id))
        return {**dict(r), "expected": json.loads(r["expected"] or "{}"), "tags": json.loads(r["tags"] or "[]")} if r else None

    def testset_update_item(self, set_id: str, item_id: str, expected: dict, tags: Optional[list] = None) -> None:
        self._exec("UPDATE testset_items SET expected=?, tags=COALESCE(?,tags) WHERE set_id=? AND id=?",
                   (json.dumps(expected, ensure_ascii=False), json.dumps(tags) if tags is not None else None,
                    set_id, item_id))

    def testset_delete_item(self, set_id: str, item_id: str) -> Optional[str]:
        r = self._one("DELETE FROM testset_items WHERE set_id=? AND id=? RETURNING path", (set_id, item_id))
        return r["path"] if r else None

    def testset_run_save(self, run: dict) -> None:
        self._exec("INSERT INTO testset_runs (id,set_id,status,combos,results,rows,error,worker,created_at,finished_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status, "
                   "results=EXCLUDED.results, rows=EXCLUDED.rows, error=EXCLUDED.error, "
                   "finished_at=EXCLUDED.finished_at",
                   (run["id"], run["setId"], run["status"], json.dumps(run["combos"]),
                    json.dumps(run.get("results") or [], default=str), json.dumps(run.get("rows") or [], default=str),
                    run.get("error"), run.get("worker"), run["createdAt"], run.get("finishedAt")))

    def testset_runs(self, set_id: str, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._all("SELECT id,set_id,status,combos,results,error,created_at,finished_at "
                                           "FROM testset_runs WHERE set_id=? ORDER BY created_at DESC LIMIT ?",
                                           (set_id, limit))]

    def testset_run(self, set_id: str, run_id: str) -> Optional[dict]:
        r = self._one("SELECT * FROM testset_runs WHERE set_id=? AND id=?", (set_id, run_id))
        return dict(r) if r else None

    def testset_interrupted_runs(self) -> int:
        """Runs left 'running' by a stopped process of this host: marked failed."""
        n = 0
        for r in self._all("SELECT id, worker FROM testset_runs WHERE status='running'"):
            if not pid_alive(r["worker"]):
                n += self._exec("UPDATE testset_runs SET status='failed', error='interrupted: the service stopped', "
                                "finished_at=? WHERE id=? AND status='running'", (now(), r["id"]))
        return n
