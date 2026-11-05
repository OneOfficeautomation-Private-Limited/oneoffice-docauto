"""PostgreSQL storage: tenant schemas, encrypted files, durable jobs (resume, SKIP LOCKED), retention, SQLite migration."""
import copy
import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import SAMPLES, make_config
from docauto import db, filecrypt
from docauto.pipeline import DocumentService, ProcessOptions
from docauto.store import HOST, Store

PDF = (SAMPLES / "invoice_digital.pdf").read_bytes()


def wait_status(store, doc_id, statuses=("success", "failed", "validation"), timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        row = store.doc_row(doc_id)
        if row and row["status"] in statuses:
            return row["status"]
        time.sleep(0.1)
    raise AssertionError(f"document {doc_id} still {store.doc_row(doc_id)['status']}")


# ------------------------------------------------------------------ schemas, files
def test_each_tenant_has_its_own_schema(tmp_path):
    prefix = f"t{uuid.uuid4().hex[:8]}_"
    a = Store(str(tmp_path / "a"), tenant="default", schema_prefix=prefix)
    b = Store(str(tmp_path / "b"), tenant="acme", schema_prefix=prefix)
    assert (a.schema, b.schema) == (prefix + "default", prefix + "acme")
    a.df_save("flow", "f1", "AP", "{}")
    assert a.df_get("flow", "f1") == "{}" and b.df_get("flow", "f1") is None
    assert db.schema_for("x" * 63).startswith("docflow_x") and len(db.schema_for("x" * 63)) <= 63


def test_files_are_encrypted_at_rest_and_old_files_still_read(tmp_path):
    svc = DocumentService(make_config())
    doc = svc.submit(PDF, "inv.pdf", ProcessOptions())["documentId"]
    path = Path(svc.store._one("SELECT path FROM documents WHERE id=?", (doc,))["path"])
    raw = path.read_bytes()
    assert raw.startswith(filecrypt.MAGIC) and b"%PDF" not in raw
    assert svc.store.file_bytes(doc)[0] == PDF                                 # transparent to readers
    key_file = Path(svc.config["storage"]["data_dir"]) / "file.key"
    assert key_file.exists()
    path.write_bytes(PDF)                                                      # a file stored before encryption
    assert svc.store.file_bytes(doc)[0] == PDF


def test_key_from_the_environment_and_rotation(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    blob = filecrypt.cipher(tmp_path / "unused.key", env_key=old).encrypt(b"secret")
    rotated = filecrypt.cipher(tmp_path / "unused.key", env_key=f"{new},{old}")
    assert rotated.decrypt(blob) == b"secret" and not (tmp_path / "unused.key").exists()
    with pytest.raises(ValueError):
        filecrypt.cipher(tmp_path / "unused.key", env_key=new).decrypt(blob)


# ------------------------------------------------------------------ durable jobs
def _queue_job(svc, worker=None, heartbeat_age=None, attempts=0):
    """A job as a crashed process would leave it."""
    doc = svc.store.save_file(PDF, "inv.pdf", "application/pdf", "x")
    svc.store.set_doc_meta(doc, None, None, "api")
    job = svc.store.create_job(doc, ProcessOptions().model_dump(mode="json", by_alias=True))
    if worker:
        svc.store._exec("UPDATE jobs SET status='running', worker=?, attempts=?, "
                        "heartbeat_at=now() - make_interval(secs => ?) WHERE id=?",
                        (worker, attempts, float(heartbeat_age), job))
        svc.store.set_doc_status(doc, "processing")
    return job, doc


def test_jobs_left_queued_or_interrupted_are_resumed_on_start():
    cfg = make_config()
    crashed = DocumentService(cfg)
    queued = _queue_job(crashed)
    stale = _queue_job(crashed, worker="other-host:42:ab", heartbeat_age=3600)          # stopped heartbeating
    dead = _queue_job(crashed, worker=f"{HOST}:999999:ab", heartbeat_age=0)              # its process is gone
    alive = _queue_job(crashed, worker="other-host:43:cd", heartbeat_age=0)              # still working
    worn = _queue_job(crashed, worker="other-host:44:ef", heartbeat_age=3600, attempts=3)
    crashed.close()
    restarted = DocumentService(copy.deepcopy(cfg))
    restarted.start_workers()
    for _, doc in (queued, stale, dead):
        assert wait_status(restarted.store, doc) == "success"
    assert wait_status(restarted.store, worn[1]) == "failed"
    assert restarted.store.get_job(worn[0])["error"]["code"] == "INTERRUPTED"
    time.sleep(0.5)
    assert restarted.store.doc_row(alive[1])["status"] == "processing"                  # left to its worker
    restarted.close()


def test_several_workers_share_the_queue_without_doing_a_job_twice():
    cfg = make_config(workers=2)
    one, two = DocumentService(cfg), DocumentService(copy.deepcopy(cfg))
    jobs = [_queue_job(one) for _ in range(8)]
    one.start_workers()
    two.start_workers()
    for _, doc in jobs:
        assert wait_status(one.store, doc) == "success"
    rows = one.store._all("SELECT id, attempts, worker FROM jobs")
    assert all(r["attempts"] == 1 for r in rows)
    usage = one.store._all("SELECT document_id, COUNT(*) n FROM usage GROUP BY document_id")
    assert len(usage) == 8 and all(u["n"] == 1 for u in usage)                       # each read exactly once
    one.close()
    two.close()


def test_async_submit_is_processed_by_the_workers():
    svc = DocumentService(make_config())
    ids = svc.submit(PDF, "inv.pdf", ProcessOptions(), wait=False)
    assert wait_status(svc.store, ids["documentId"]) == "success"
    assert svc.store.get_job(ids["jobId"])["status"] == "completed"
    svc.close()


# ------------------------------------------------------------------ retention
def test_retention_deletes_old_documents_but_not_work_in_progress_or_test_sets(monkeypatch):
    monkeypatch.setenv("DOCAUTO_FOLDER_WATCHER", "false")
    from docauto.docflow import DocFlowService
    svc = DocumentService(make_config())
    df = DocFlowService(svc)
    old, waiting, recent = (svc.submit(PDF, f"{n}.pdf", ProcessOptions(), wait=True)["documentId"]
                            for n in ("old", "waiting", "recent"))
    svc.store.set_doc_status(waiting, "validation")
    long_ago = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    for d in (old, waiting):
        svc.store._exec("UPDATE documents SET created_at=? WHERE id=?", (long_ago, d))
    old_file = Path(svc.store._one("SELECT path FROM documents WHERE id=?", (old,))["path"])
    s = df.testsets.create("Kept", "invoice")
    item = df.testsets.add_file(s["id"], PDF, "kept.pdf", {"total": 11500.0})
    svc.config["storage"]["retention_days"] = 0
    assert df.enforce_retention(force=True) == 0                              # 0 = keep forever
    svc.config["storage"]["retention_days"] = "30"
    assert df.enforce_retention(force=True) == 1
    assert svc.store.doc_row(old) is None and not old_file.exists()
    assert svc.store._one("SELECT COUNT(*) n FROM jobs WHERE document_id=?", (old,))["n"] == 0
    assert svc.store.doc_row(waiting) and svc.store.doc_row(recent)
    assert df.testsets.item_file(s["id"], item["id"])[0] == PDF
    assert svc.store._one("SELECT COUNT(*) n FROM usage WHERE document_id=?", (old,))["n"] == 1   # billing stays
    assert df.enforce_retention() == 0                                        # at most once a day
    df.stop_watcher()


# ------------------------------------------------------------------ SQLite -> PostgreSQL
OLD_SCHEMA = """
CREATE TABLE jobs (id TEXT PRIMARY KEY, document_id TEXT, status TEXT, request TEXT, error TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE documents (id TEXT PRIMARY KEY, file_name TEXT, mime_type TEXT, sha256 TEXT, path TEXT,
  result TEXT, schema_id TEXT, review_status TEXT, created_at TEXT, instance_id TEXT, instance_version INTEGER,
  status TEXT, source TEXT, processing_ms INTEGER, updated_at TEXT, validated_by TEXT, field_count INTEGER,
  corrected_count INTEGER, doctype_id TEXT, parent_id TEXT, flags TEXT, sla_notified INTEGER);
CREATE TABLE schemas (id TEXT PRIMARY KEY, name TEXT, body TEXT, updated_at TEXT);
CREATE TABLE instances (id TEXT PRIMARY KEY, name TEXT, body TEXT, status TEXT, version INTEGER, created_at TEXT,
  updated_at TEXT);
CREATE TABLE corrections (id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT, document_id TEXT, field TEXT,
  original TEXT, corrected TEXT, reviewer TEXT, ts TEXT);
CREATE TABLE df_objects (kind TEXT, id TEXT, name TEXT, body TEXT, status TEXT, updated_at TEXT, PRIMARY KEY (kind, id));
CREATE TABLE df_reftables (id TEXT PRIMARY KEY, name TEXT, columns TEXT, rows TEXT, updated_at TEXT);
CREATE TABLE df_dedupe (flow_id TEXT, key TEXT, document_id TEXT, ts TEXT);
CREATE TABLE df_deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, flow_id TEXT, document_id TEXT, delivery TEXT,
  type TEXT, ok INTEGER, detail TEXT, ts TEXT);
CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, job_id TEXT, document_id TEXT,
  provider_id TEXT, option_pack TEXT, capability TEXT, pages INTEGER, credits REAL, runs_locally INTEGER,
  regions TEXT, ts TEXT);
"""


def _sqlite_install(root: Path, tenant_flow: str) -> str:
    """A data folder as DocFlow 3 left it: SQLite database, plain files."""
    files = root / "files"
    files.mkdir(parents=True)
    (files / "d1_inv.pdf").write_bytes(PDF)
    con = sqlite3.connect(root / "docauto.db")
    con.executescript(OLD_SCHEMA)
    result = {"documentId": "d1", "fields": [{"name": "total", "value": 11500.0, "confidence": 0.99}],
              "text": "Total: 11,500.00 فاتورة"}
    con.execute("INSERT INTO documents (id,file_name,mime_type,path,result,created_at,instance_id,status,doctype_id,"
                "review_status,field_count) VALUES ('d1','inv.pdf','application/pdf',?,?,?,?, 'success','invoice',"
                "'not_required',1)", (str(files / "d1_inv.pdf"), json.dumps(result, ensure_ascii=False),
                                      datetime.now(timezone.utc).isoformat(), tenant_flow))
    con.execute("INSERT INTO jobs VALUES ('j1','d1','completed','{}',NULL,'2026-11-01','2026-11-01')")
    con.execute("INSERT INTO df_objects VALUES ('flow',?,'AP invoices',?, 'live','2026-11-01')",
                (tenant_flow, json.dumps({"id": tenant_flow, "name": "AP invoices", "docTypes": ["invoice"]})))
    con.execute("INSERT INTO usage (tenant_id,job_id,document_id,provider_id,option_pack,capability,pages,credits,"
                "runs_locally,regions,ts) VALUES ('default','j1','d1','pdf-text','core','ocr_printed',1,0.0,1,'',?)",
                (datetime.now(timezone.utc).isoformat(),))
    con.execute("INSERT INTO corrections (instance_id,document_id,field,original,corrected,reviewer,ts) "
                "VALUES ('invoice','d1','total','1.0','11500.0','qa','2026-11-01')")
    con.execute("INSERT INTO df_deliveries (flow_id,document_id,delivery,type,ok,detail,ts) "
                "VALUES (?,'d1','ERP','webhook',1,'HTTP 200','2026-11-01')", (tenant_flow,))
    con.execute("INSERT INTO df_dedupe VALUES (?,'k','d1','2026-11-01')", (tenant_flow,))
    con.execute("INSERT INTO df_reftables VALUES ('suppliers','Suppliers','[\"vat\"]','[{\"vat\": \"3\"}]','x')")
    con.commit()
    con.close()
    return str(root)


def test_sqlite_install_is_copied_to_postgres_once_per_tenant(tmp_path):
    from docauto import migrate_sqlite
    base = tmp_path / "data"
    _sqlite_install(base, "ap")
    _sqlite_install(base / "tenants" / "acme", "acme-ap")
    url, prefix = db.resolve_url(None, base), f"m{uuid.uuid4().hex[:8]}_"
    first = migrate_sqlite.migrate(base, url, schema_prefix=prefix)
    assert [r["tenant"] for r in first] == ["default", "acme"]
    assert first[0]["copied"]["documents"] == 1 and first[0]["copied"]["usage"] == 1
    again = migrate_sqlite.migrate(base, url, schema_prefix=prefix)                     # idempotent
    assert all(r["alreadyMigrated"] for r in again) and again[0]["copied"]["usage"] == 0
    store = Store(str(base), database_url=url, schema_prefix=prefix)
    for table in ("documents", "jobs", "usage", "corrections", "df_deliveries", "df_dedupe", "df_objects"):
        assert store._one(f"SELECT COUNT(*) n FROM {table}")["n"] == 1, table
    assert store.file_bytes("d1")[0] == PDF                                              # file left in place
    assert store.get_result("d1").text.endswith("فاتورة")
    assert store.ref_get("suppliers")["rows"] == [{"vat": "3"}]
    acme = Store(str(base / "tenants" / "acme"), database_url=url, tenant="acme", schema_prefix=prefix)
    assert acme.df_get("flow", "acme-ap") and not acme.df_get("flow", "ap")             # tenants stay apart
    store.delivery_log("ap", "d1", "ERP", "webhook", True, "HTTP 200")                  # ids continue after the copy
    assert [d["id"] for d in store.delivery_list("ap")] == [2, 1]
    assert migrate_sqlite.main([str(base), "--dry-run"]) == 0
