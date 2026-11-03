"""Persistence: jobs, documents/results, schemas, usage metering (SQLite by default).

Swap for the host platform's database by re-implementing this class.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import DocumentResult, FieldSchema

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, document_id TEXT, status TEXT, request TEXT, error TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, file_name TEXT, mime_type TEXT, sha256 TEXT, path TEXT,
  result TEXT, schema_id TEXT, review_status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS schemas (id TEXT PRIMARY KEY, name TEXT, body TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS instances (
  id TEXT PRIMARY KEY, name TEXT, body TEXT, status TEXT, version INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT, document_id TEXT, field TEXT,
  original TEXT, corrected TEXT, reviewer TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS df_objects (
  kind TEXT, id TEXT, name TEXT, body TEXT, status TEXT, updated_at TEXT, PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS df_reftables (id TEXT PRIMARY KEY, name TEXT, columns TEXT, rows TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS df_dedupe (flow_id TEXT, key TEXT, document_id TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS df_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, flow_id TEXT, document_id TEXT, delivery TEXT, type TEXT,
  ok INTEGER, detail TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, job_id TEXT, document_id TEXT,
  provider_id TEXT, option_pack TEXT, capability TEXT, pages INTEGER, credits REAL,
  runs_locally INTEGER, regions TEXT, ts TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, data_dir: str):
        self.dir = Path(data_dir)
        (self.dir / "files").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.dir / "docauto.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA_SQL)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after v1.0 (safe to run on every start)."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(documents)").fetchall()}
        for name, typ in (("instance_id", "TEXT"), ("instance_version", "INTEGER"), ("status", "TEXT"),
                          ("source", "TEXT"), ("processing_ms", "INTEGER"), ("updated_at", "TEXT"),
                          ("validated_by", "TEXT"), ("field_count", "INTEGER"), ("corrected_count", "INTEGER"),
                          ("doctype_id", "TEXT"), ("parent_id", "TEXT"), ("flags", "TEXT"), ("sla_notified", "INTEGER")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE documents ADD COLUMN {name} {typ}")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_doc_instance ON documents(instance_id, status)")
        self.db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    # documents ---------------------------------------------------------------
    def save_file(self, data: bytes, file_name: str, mime: str, sha: str) -> str:
        doc_id = uuid.uuid4().hex
        path = self.dir / "files" / f"{doc_id}_{Path(file_name).name}"
        path.write_bytes(data)
        self._exec("INSERT INTO documents (id,file_name,mime_type,sha256,path,created_at) VALUES (?,?,?,?,?,?)",
                   (doc_id, file_name, mime, sha, str(path), now()))
        return doc_id

    def file_bytes(self, doc_id: str) -> Optional[tuple[bytes, str, str]]:
        r = self._one("SELECT path,file_name,mime_type FROM documents WHERE id=?", (doc_id,))
        if not r:
            return None
        return Path(r["path"]).read_bytes(), r["file_name"], r["mime_type"]

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
        self._exec("INSERT OR REPLACE INTO df_objects VALUES (?,?,?,?,?,?)", (kind, obj_id, name, body, status, now()))

    def df_get(self, kind: str, obj_id: str) -> Optional[str]:
        r = self._one("SELECT body FROM df_objects WHERE kind=? AND id=?", (kind, obj_id))
        return r["body"] if r else None

    def df_list(self, kind: str) -> list[str]:
        return [r["body"] for r in self._all("SELECT body FROM df_objects WHERE kind=? ORDER BY name", (kind,))]

    def df_delete(self, kind: str, obj_id: str) -> None:
        self._exec("DELETE FROM df_objects WHERE kind=? AND id=?", (kind, obj_id))

    def ref_save(self, ref_id: str, name: str, columns: list[str], rows: list[dict]) -> None:
        self._exec("INSERT OR REPLACE INTO df_reftables VALUES (?,?,?,?,?)",
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
        self._exec("DELETE FROM df_dedupe WHERE flow_id=? AND document_id=?", (flow_id, doc_id))
        self._exec("INSERT INTO df_dedupe VALUES (?,?,?,?)", (flow_id, key, doc_id, now()))

    def delivery_log(self, flow_id: str, doc_id: str, name: str, typ: str, ok: bool, detail: str) -> None:
        self._exec("INSERT INTO df_deliveries (flow_id,document_id,delivery,type,ok,detail,ts) VALUES (?,?,?,?,?,?,?)",
                   (flow_id, doc_id, name, typ, int(ok), detail[:500], now()))

    def delivery_list(self, flow_id: Optional[str], limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM df_deliveries" + (" WHERE flow_id=?" if flow_id else "") + " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self._all(sql, ((flow_id, limit) if flow_id else (limit,)))]

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
        self._exec("UPDATE documents SET status=?, processing_ms=COALESCE(?,processing_ms), "
                   "validated_by=COALESCE(?,validated_by), field_count=COALESCE(?,field_count), "
                   "corrected_count=COALESCE(?,corrected_count), updated_at=? WHERE id=?",
                   (status, processing_ms, validated_by, field_count, corrected_count, now(), doc_id))

    def doc_row(self, doc_id: str) -> Optional[dict]:
        r = self._one("SELECT id,file_name,mime_type,created_at,updated_at,status,instance_id,instance_version,source,"
                      "processing_ms,validated_by,field_count,corrected_count,review_status,doctype_id,parent_id,flags "
                      "FROM documents WHERE id=?",
                      (doc_id,))
        return dict(r) if r else None

    def instance_documents(self, instance_id: Optional[str], statuses: Optional[list[str]] = None,
                           limit: int = 200, offset: int = 0) -> list[dict]:
        sql = ("SELECT id,file_name,created_at,updated_at,status,instance_id,instance_version,source,processing_ms,"
               "validated_by,field_count,corrected_count,doctype_id,parent_id,flags FROM documents "
               "WHERE instance_id IS NOT NULL")
        args: list = []
        if instance_id:
            sql += " AND instance_id=?"
            args.append(instance_id)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        return [dict(r) for r in self._all(sql, tuple(args + [limit, offset]))]

    def next_for_validation(self, instance_id: Optional[str], after: Optional[str] = None) -> Optional[str]:
        sql = "SELECT id FROM documents WHERE status='validation'"
        args: list = []
        if instance_id:
            sql += " AND instance_id=?"
            args.append(instance_id)
        if after:
            sql += " AND id<>?"
            args.append(after)
        r = self._one(sql + " ORDER BY created_at LIMIT 1", tuple(args))
        return r["id"] if r else None

    def instance_stats(self, instance_id: Optional[str]) -> dict:
        where, args = (("WHERE instance_id=? AND COALESCE(status,'')<>'split'", (instance_id,)) if instance_id
                       else ("WHERE instance_id IS NOT NULL AND COALESCE(status,'')<>'split'", ()))
        by = {r["status"]: r["n"] for r in self._all(f"SELECT status, COUNT(*) n FROM documents {where} GROUP BY status", args)}
        agg = self._one(f"SELECT AVG(processing_ms) ms, SUM(COALESCE(field_count,0)) f, "
                        f"SUM(COALESCE(corrected_count,0)) c FROM documents {where} AND status IN ('success','validated')",
                        args)
        daily = [dict(r) for r in self._all(
            f"SELECT substr(created_at,1,10) day, COUNT(*) total, "
            f"SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) stp, "
            f"SUM(CASE WHEN status IN ('validation','validated','invalid') THEN 1 ELSE 0 END) human, "
            f"SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed "
            f"FROM documents {where} GROUP BY day ORDER BY day DESC LIMIT 14", args)][::-1]
        pages = self._one(f"SELECT COALESCE(SUM(u.pages),0) p, COALESCE(SUM(u.credits),0) cr FROM usage u "
                          f"JOIN documents d ON d.id=u.document_id {where.replace(" AND COALESCE(status,'')<>'split'", '').replace('instance_id', 'd.instance_id')} "
                          f"AND u.capability IN ('ocr_printed','tables','ocr_handwriting','layout','key_value')", args)
        done = sum(by.get(k, 0) for k in ("success", "validation", "validated", "invalid"))
        fields, corrected = (agg["f"] or 0), (agg["c"] or 0)
        return {"byStatus": {k: by.get(k, 0) for k in self.DOC_STATUSES}, "total": sum(by.values()),
                "processed": done, "stpRate": round(by.get("success", 0) / done, 4) if done else None,
                "fieldAccuracy": round(1 - corrected / fields, 4) if fields else None,
                "avgProcessingMs": int(agg["ms"]) if agg["ms"] else None, "pages": pages["p"],
                "credits": pages["cr"], "daily": daily}

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
        self._exec("INSERT OR REPLACE INTO instances VALUES (?,?,?,?,?,?,?)",
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

    # jobs --------------------------------------------------------------------
    def create_job(self, document_id: str, request: dict) -> str:
        job_id = uuid.uuid4().hex
        self._exec("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                   (job_id, document_id, "queued", json.dumps(request), None, now(), now()))
        return job_id

    def update_job(self, job_id: str, status: str, error: Optional[dict] = None) -> None:
        self._exec("UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                   (status, json.dumps(error) if error else None, now(), job_id))

    def get_job(self, job_id: str) -> Optional[dict]:
        r = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not r:
            return None
        d = dict(r)
        d["request"] = json.loads(d["request"] or "{}")
        d["error"] = json.loads(d["error"]) if d["error"] else None
        return d

    # schemas -----------------------------------------------------------------
    def save_schema(self, schema: FieldSchema) -> FieldSchema:
        schema.id = schema.id or uuid.uuid4().hex[:12]
        self._exec("INSERT OR REPLACE INTO schemas VALUES (?,?,?,?)",
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
                     runs_locally: bool, regions: list[str]) -> None:
        self._exec("INSERT INTO usage (tenant_id,job_id,document_id,provider_id,option_pack,capability,pages,"
                   "credits,runs_locally,regions,ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (tenant_id, job_id, document_id, provider_id, option_pack, capability, pages, credits,
                    int(runs_locally), ",".join(regions), now()))

    def usage_summary(self, group_by: str = "provider_id") -> list[dict[str, Any]]:
        if group_by not in ("provider_id", "option_pack", "tenant_id", "capability"):
            raise ValueError("invalid group_by")
        rows = self._all(f"SELECT {group_by} AS k, COUNT(*) AS calls, SUM(pages) AS pages, "
                         f"SUM(credits) AS credits, SUM(1-runs_locally) AS outbound_calls "
                         f"FROM usage GROUP BY {group_by} ORDER BY credits DESC")
        return [dict(r) for r in rows]
