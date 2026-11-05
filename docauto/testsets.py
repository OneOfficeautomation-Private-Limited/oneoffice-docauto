"""Test sets: the quality gate for a document type.

A test set is a list of documents of one document type with their correct values (the ground truth): reviewed
documents copied from a process (the values the reviewer approved), files uploaded with expected values, or the
synthetic seed set. A run evaluates one or more engine combinations (OCR engine x extraction engine) over the set
and keeps the result: per-field accuracy, straight-through rate, documents wrongly auto-approved, cost estimate and
time per page (the metrics of docauto.benchmark). Run documents are processed like Test lab documents: nothing is
filed in a process, delivered or kept (usage is metered).

Test-set files are copies (encrypted like every stored file) under <data dir>/testsets/<set id>/, apart from the
documents table, so retention never deletes them.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import benchmark as bm
from . import costs
from .errors import DocAutoError, InvalidInput
from .preprocess import sniff_mime
from .store import worker_id

log = logging.getLogger("docauto.testsets")
ROOT = Path(__file__).resolve().parent.parent
SEED_NAME = "Synthetic invoices (seed)"
MAX_COMBOS = 6
# per-document rows are scored by docauto.benchmark (snake_case); the API shows them in camelCase
ROW_KEYS = {"document": "fileName", "engine": "label", "char_accuracy": "charAccuracy", "ocr_confidence": "ocrConfidence",
            "low_confidence_pages": "lowConfidencePages", "review_required": "reviewRequired",
            "review_reasons": "reviewReasons", "straight_through": "straightThrough",
            "silent_error": "wronglyAutoApproved", "cloud_pages": "cloudPages"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", Path(name or "document").name)[:120] or "document"


def combo_label(c: dict) -> str:
    return f"{c['ocr']} × {c['extraction']}"


class TestSets:
    def __init__(self, df):
        self.df, self.svc, self.store = df, df.svc, df.store
        self.dir = Path(self.store.dir) / "testsets"
        self._runner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="docflow-testset")
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- sets
    def _set(self, set_id: str) -> dict:
        s = self.store.testset_get(set_id)
        if not s:
            raise InvalidInput(f"unknown test set '{set_id}'")
        return s

    def _out(self, s: dict, with_last_run: bool = True) -> dict:
        out = {"id": s["id"], "name": s["name"], "docType": s["doctype_id"], "description": s["description"] or "",
               "itemCount": s["item_count"], "createdAt": s["created_at"], "updatedAt": s["updated_at"]}
        if with_last_run:
            runs = self.store.testset_runs(s["id"], 1)
            out["lastRun"] = self._run_out(runs[0], rows=False) if runs else None
        return out

    def create(self, name: str, doc_type: str, description: str = "") -> dict:
        if not (name or "").strip():
            raise InvalidInput("name is required")
        self.df.doctype(doc_type)
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "testset"
        with self._lock:
            set_id, n = base, 2
            while self.store.testset_get(set_id):
                set_id, n = f"{base}-{n}", n + 1
            self.store.testset_save(set_id, name.strip(), doc_type, description or "")
        return self.get(set_id)

    def update(self, set_id: str, name: Optional[str] = None, description: Optional[str] = None) -> dict:
        s = self._set(set_id)
        self.store.testset_save(set_id, (name or s["name"]).strip(), s["doctype_id"],
                                s["description"] if description is None else description)
        return self.get(set_id)

    def list(self, doc_type: Optional[str] = None) -> list[dict]:
        return [self._out(s) for s in self.store.testset_list(doc_type)]

    def get(self, set_id: str) -> dict:
        s = self._set(set_id)
        return {**self._out(s), "items": [self._item_out(i) for i in self.store.testset_items(set_id)]}

    def delete(self, set_id: str) -> None:
        self._set(set_id)
        for p in self.store.testset_delete(set_id):
            Path(p).unlink(missing_ok=True)
        folder = self.dir / set_id
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()

    # ---------------------------------------------------------------- items
    @staticmethod
    def _item_out(i: dict) -> dict:
        return {"id": i["id"], "setId": i["set_id"], "fileName": i["file_name"], "mimeType": i["mime_type"],
                "expected": i["expected"], "tags": i["tags"], "source": i["source"],
                "sourceDocumentId": i["source_document_id"], "createdAt": i["created_at"]}

    def _fields(self, set_id: str) -> dict[str, str]:
        """Scalar fields of the set's document type: {name: type} (tables are not scored)."""
        dt = self.df.doctype(self._set(set_id)["doctype_id"])
        return {f.name: f.type for f in dt.fields if f.type not in ("table", "object")}

    def _check_expected(self, set_id: str, expected: dict) -> dict:
        fields = self._fields(set_id)
        unknown = [k for k in expected if k not in fields]
        if unknown:
            raise InvalidInput(f"not fields of the document type: {', '.join(unknown)}")
        out = {k: v for k, v in expected.items() if v not in (None, "", [])}
        if not out:
            raise InvalidInput("give at least one expected value")
        return out

    def _add(self, set_id: str, data: bytes, file_name: str, expected: dict, tags: list[str], source: str,
             source_doc: Optional[str] = None) -> dict:
        if not data:
            raise InvalidInput("empty file")
        item_id = uuid.uuid4().hex[:12]
        path = self.dir / set_id / f"{item_id}_{_safe(file_name)}"
        self.store.write_file(path, data)
        item = {"id": item_id, "set_id": set_id, "file_name": file_name, "mime_type": sniff_mime(data, file_name),
                "path": str(path), "expected": expected, "tags": [str(t) for t in tags or []], "source": source,
                "source_document_id": source_doc}
        try:
            self.store.testset_add_item(item)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return self._item_out(self.store.testset_item(set_id, item_id))

    def add_document(self, set_id: str, document_id: str, tags: Optional[list[str]] = None) -> dict:
        """Copy a reviewed (validated) document: its file, and the values the reviewer approved as ground truth."""
        s = self._set(set_id)
        row = self.store.doc_row(document_id)
        if not row:
            raise InvalidInput(f"unknown document '{document_id}'")
        if row["status"] != "validated":
            raise InvalidInput(f"only reviewed documents can be added (this one is '{row['status']}')")
        if row.get("doctype_id") and row["doctype_id"] != s["doctype_id"]:
            raise InvalidInput(f"the document is a '{row['doctype_id']}', the set is for '{s['doctype_id']}'")
        res = self.store.get_result(document_id)
        f = self.store.file_bytes(document_id)
        if not res or not f:
            raise InvalidInput("the document has no result or file any more")
        fields = self._fields(set_id)
        expected = {x.name: x.value for x in res.fields if x.name in fields and x.value not in (None, "", [])}
        if not expected:
            raise InvalidInput("the document has no approved values for this document type")
        return self._add(set_id, f[0], f[1], expected, tags or ["reviewed"], "document", document_id)

    def add_file(self, set_id: str, data: bytes, file_name: str, expected: dict,
                 tags: Optional[list[str]] = None) -> dict:
        return self._add(set_id, data, file_name, self._check_expected(set_id, expected), tags or [], "upload")

    def update_item(self, set_id: str, item_id: str, expected: dict, tags: Optional[list[str]] = None) -> dict:
        if not self.store.testset_item(set_id, item_id):
            raise InvalidInput(f"unknown item '{item_id}'")
        self.store.testset_update_item(set_id, item_id, self._check_expected(set_id, expected), tags)
        return self._item_out(self.store.testset_item(set_id, item_id))

    def delete_item(self, set_id: str, item_id: str) -> None:
        path = self.store.testset_delete_item(set_id, item_id)
        if path is None:
            raise InvalidInput(f"unknown item '{item_id}'")
        Path(path).unlink(missing_ok=True)

    def item_file(self, set_id: str, item_id: str) -> tuple[bytes, str, str]:
        i = self.store.testset_item(set_id, item_id)
        if not i:
            raise InvalidInput(f"unknown item '{item_id}'")
        return self.store.read_file(i["path"]), i["file_name"], i["mime_type"]

    # ---------------------------------------------------------------- seed (synthetic, no real data)
    def seed(self, count: int = 3, bilingual: bool = True) -> dict:
        """A test set for the "invoice" document type from the synthetic benchmark generator
        (samples/make_benchmark_set.py): clean, scanned, fax and digital-PDF invoices, plus Arabic/English ones."""
        self.df.doctype("invoice")
        sys.path.insert(0, str(ROOT / "samples"))
        try:
            import make_benchmark_set as gen
        finally:
            sys.path.remove(str(ROOT / "samples"))
        existing = next((s for s in self.store.testset_list("invoice") if s["name"] == SEED_NAME), None)
        s = self.get(existing["id"]) if existing else self.create(SEED_NAME, "invoice",
                                                                  "Generated invoices (no real data): clean, scan, "
                                                                  "fax, PDF and Arabic/English bilingual pages.")
        variants = ("clean", "scan", "fax", "pdf") + (("bilingual",) if bilingual else ())
        with tempfile.TemporaryDirectory(prefix="docflow-seed-") as tmp:
            paths = gen.make_set(Path(tmp), count=max(1, min(int(count), 20)), seed=11, variants=variants)
            have = {i["fileName"] for i in s["items"]}
            for p in paths:
                if p.name in have:
                    continue
                truth = json.loads(p.with_suffix(".truth.json").read_text(encoding="utf-8"))
                self._add(s["id"], p.read_bytes(), p.name, truth["fields"], truth.get("tags", []), "seed")
        return self.get(s["id"])

    # ---------------------------------------------------------------- runs
    def _run_out(self, r: dict, rows: bool = True) -> dict:
        out = {"id": r["id"], "setId": r["set_id"], "status": r["status"], "combos": json.loads(r["combos"] or "[]"),
               "results": json.loads(r["results"] or "[]"), "error": r["error"], "createdAt": r["created_at"],
               "finishedAt": r["finished_at"]}
        if rows:
            out["rows"] = [{ROW_KEYS.get(k, k): v for k, v in row.items()} for row in json.loads(r.get("rows") or "[]")]
        return out

    def runs(self, set_id: str, limit: int = 50) -> list[dict]:
        self._set(set_id)
        return [self._run_out(r, rows=False) for r in self.store.testset_runs(set_id, limit)]

    def run_detail(self, set_id: str, run_id: str) -> dict:
        r = self.store.testset_run(set_id, run_id)
        if not r:
            raise InvalidInput(f"unknown run '{run_id}'")
        return self._run_out(r)

    def mark_interrupted(self) -> None:
        try:
            self.store.testset_interrupted_runs()
        except Exception:
            log.exception("could not check for interrupted test-set runs")

    def start_run(self, set_id: str, combos: list[dict], flow_id: Optional[str] = None, wait: bool = False) -> dict:
        s = self._set(set_id)
        if not s["item_count"]:
            raise InvalidInput("the test set has no documents yet")
        clean = []
        for c in combos or [{"ocr": "auto", "extraction": "auto"}]:
            c = {"ocr": str(c.get("ocr") or "auto").strip(), "extraction": str(c.get("extraction") or "auto").strip()}
            if c not in clean:
                clean.append(c)
        if len(clean) > MAX_COMBOS:
            raise InvalidInput(f"at most {MAX_COMBOS} engine combinations per run")
        fl = self.df.flow(flow_id) if flow_id else None
        run = {"id": uuid.uuid4().hex[:12], "setId": set_id, "status": "running", "combos": clean, "results": [],
               "rows": [], "error": None, "worker": worker_id(), "createdAt": _now(), "finishedAt": None}
        self.store.testset_run_save(run)
        if wait:
            self._execute(run, fl)
        else:
            self._runner.submit(self._execute, run, fl)
        return self.run_detail(set_id, run["id"])

    def _execute(self, run: dict, fl) -> None:
        try:
            s = self._set(run["setId"])
            dt = self.df.doctype(s["doctype_id"])
            items = self.store.testset_items(run["setId"])
            schema = dt.schema()
            types = {f.name: f.type for f in dt.fields}
            ts = bm.TestSet(s["name"], self.dir / s["id"], [], schema, dt.languages or ["en"])
            for combo in run["combos"]:
                why = self._unavailable(combo)
                rows = [] if why else [self._evaluate(ts, dt, fl, combo, it, types) for it in items]
                run["rows"] += rows
                run["results"].append(self._summary(combo, rows, why))
                self.store.testset_run_save(run)      # progress is visible while the run goes on
            run["status"] = "completed"
        except Exception as e:
            log.exception("test-set run %s failed", run["id"])
            run["status"], run["error"] = "failed", f"{type(e).__name__}: {e}"
        run["finishedAt"] = _now()
        self.store.testset_run_save(run)

    def _unavailable(self, combo: dict) -> Optional[str]:
        for eid in (combo["ocr"], combo["extraction"]):
            if eid == "auto":
                continue
            p = self.svc.registry.get(eid)
            if p is None:
                return f"unknown engine '{eid}'"
            ok, why = self.svc.policy.check(p.info)
            if not ok:
                return f"{eid}: {why}"
            if not p.available:
                return f"{eid}: {p.reason or 'not available'}"
        return None

    def _evaluate(self, ts, dt, fl, combo: dict, item: dict, types: dict) -> dict:
        from .pipeline import ProcessOptions
        opt = ProcessOptions(ocrProvider=combo["ocr"], extractionProvider=combo["extraction"], strictProvider=True,
                             languages=dt.languages or ["en"], schema=dt.schema(), tenantId=self.df.tenant,
                             source="testset", **(self.df._options(fl, dt, "testset").model_dump(
                                 include={"fieldThreshold", "docThreshold", "fieldThresholds", "maskPii",
                                          "cloudImages", "einvoiceFirst"}) if fl else {}))
        doc = bm.TestDoc(Path(item["file_name"]), {k: v for k, v in item["expected"].items() if k in types},
                         "", item["tags"])
        row: dict[str, Any] = {"itemId": item["id"], "document": item["file_name"], "engine": combo_label(combo),
                               "ocr": combo["ocr"], "extraction": combo["extraction"], "tags": item["tags"]}
        t0 = time.perf_counter()
        doc_id = None
        try:
            data = self.store.read_file(item["path"])
            ids = self.svc.submit(data, item["file_name"], opt, wait=True)
            doc_id = ids["documentId"]
            job = self.store.get_job(ids["jobId"])
            if job["status"] == "failed":
                raise DocAutoError((job["error"] or {}).get("message", "processing failed"))
            result = self.store.get_result(doc_id)
            used = [s.providerId for s in result.provenance.steps if s.stage == "extract"]
            if combo["extraction"] != "auto" and used and used[-1] != combo["extraction"]:
                raise DocAutoError(f"extraction ran on {used[-1]}, not {combo['extraction']}")
            row.update(bm._score_doc(self.svc, ts, doc, result, time.perf_counter() - t0))
            row["costUsd"], row["costKnown"] = self._cost(result)
        except DocAutoError as e:
            row.update({"seconds": round(time.perf_counter() - t0, 3), "error": e.message, "pages": 0,
                        "fields": {n: {"want": w, "got": None, "ok": False} for n, w in doc.fields.items()},
                        "straight_through": False, "silent_error": False, "cloud_pages": 0, "credits": 0,
                        "costUsd": 0.0, "costKnown": True})
        finally:
            if doc_id:
                self.store.delete_document(doc_id)
        return row

    @staticmethod
    def _cost(result) -> tuple[float, bool]:
        """USD at list prices (docauto.costs) for what this document used; known=False when an engine has none."""
        usd, known = 0.0, True
        for s in result.provenance.steps:
            use = {"digitize": "ocr", "extract": "extract"}.get(s.stage)
            if not use or not s.pages:
                continue
            price = costs.price_per_1000(s.providerId, use, s.model)
            if price is None:
                known = False
                continue
            usd += price * s.pages / 1000
        return round(usd, 6), known

    @staticmethod
    def _summary(combo: dict, rows: list[dict], skipped: Optional[str]) -> dict:
        out = {"ocr": combo["ocr"], "extraction": combo["extraction"], "label": combo_label(combo),
               "skipped": skipped}
        if skipped or not rows:
            return out
        s = bm.summarize(rows)
        o = s["overall"]
        pages = o["pages"]
        secs = sum(r["seconds"] for r in rows if not r.get("error"))
        cost = sum(r.get("costUsd") or 0 for r in rows)
        return {**out, "documents": o["documents"], "pages": pages, "errors": o["errors"],
                "fieldAccuracy": o["field_accuracy"], "byField": s["by_field"],
                "straightThrough": o["straight_through"], "reviewRate": o["review_rate"],
                "wronglyAutoApproved": sum(1 for r in rows if r.get("silent_error")),
                "wronglyAutoApprovedRate": o["silent_errors"], "charAccuracy": o["char_accuracy"],
                "credits": o["credits"], "costUsd": round(cost, 4),
                "costPer1000PagesUsd": round(cost * 1000 / pages, 2) if pages else None,
                "costKnown": all(r.get("costKnown", True) for r in rows),
                "secondsPerPage": round(secs / pages, 3) if pages else None, "p95Seconds": o["turnaround_p95_s"]}
