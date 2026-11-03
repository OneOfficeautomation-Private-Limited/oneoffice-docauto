"""Learning instances: the Document Automation feature (modelled on Automation Anywhere).

A learning instance is a saved, versioned configuration for ONE kind of document:
document type + languages + engine option + fields/tables + validation rules +
confidence thresholds + optional input/output folders. Documents are uploaded
*into an instance* (from the UI, a bot action, the API or a watched folder) and
move through a lifecycle:

    queued -> processing -> success          (straight-through, no human needed)
                         -> validation -> validated | invalid   (human in the loop)
                         -> failed

Reviewer corrections are recorded; the instance *learns* new field labels from
them (so the next document with the same layout is extracted correctly) and passes
recent corrections to AI extractors as guidance.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .errors import InvalidInput
from .models import FieldDef, FieldSchema
from .pipeline import DocumentService, ProcessOptions

log = logging.getLogger("docauto.instances")

DocType = Literal["invoice", "purchase_order", "receipt", "bank_statement", "id_document", "user_defined"]
SUPPORTED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EngineSettings(BaseModel):
    option: Literal["tier1_opensource", "tier2_cloud", "tier3_ai"] = "tier1_opensource"
    ocrProvider: str = "auto"
    extractionProvider: str = "auto"
    capabilities: list[str] = Field(default_factory=lambda: ["ocr_printed"])


class FolderSettings(BaseModel):
    inputFolder: str = ""
    outputFolder: str = ""
    pollSeconds: int = 30


class LearningSettings(BaseModel):
    enabled: bool = True
    learnedAliases: dict[str, list[str]] = Field(default_factory=dict)


class LearningInstance(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    documentType: DocType = "user_defined"
    languages: list[str] = Field(default_factory=lambda: ["en", "ar"])
    engine: EngineSettings = Field(default_factory=EngineSettings)
    fields: list[FieldDef] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    fieldConfidence: float = 0.85
    documentConfidence: float = 0.80
    folders: FolderSettings = Field(default_factory=FolderSettings)
    learning: LearningSettings = Field(default_factory=LearningSettings)
    status: Literal["draft", "published"] = "draft"
    version: int = 1
    createdAt: str = ""
    updatedAt: str = ""
    publishedAt: Optional[str] = None

    def schema_for_processing(self) -> FieldSchema:
        """Field schema with learned labels merged into aliases."""
        fields = []
        learned = self.learning.learnedAliases if self.learning.enabled else {}
        for f in self.fields:
            f2 = f.model_copy(deep=True)
            for a in learned.get(f.name, []):
                if a not in f2.aliases:
                    f2.aliases.append(a)
            fields.append(f2)
        return FieldSchema(id=f"instance:{self.id}", name=self.name, description=self.description,
                           documentLanguages=self.languages, fields=fields, validation=self.validation)


# ----------------------------------------------------------------- templates
def _f(name, type_="string", label=None, required=False, aliases=None, regex=None, fmt=None, enum=None,
       description=None, columns=None) -> dict:
    d = {"name": name, "type": type_, "label": label, "required": required, "aliases": aliases or []}
    if regex:
        d["regex"] = regex
    if fmt:
        d["format"] = fmt
    if enum:
        d["enumValues"] = enum
    if description:
        d["description"] = description
    if columns:
        d["columns"] = columns
    return d


TEMPLATES: dict[str, dict[str, Any]] = {
    "invoice": {
        "title": "Invoices", "description": "Supplier / tax invoices (ZATCA-style VAT invoices, English and Arabic).",
        "fields": [
            _f("invoice_number", label="Invoice Number / رقم الفاتورة", required=True, aliases=["Invoice No", "Bill No"]),
            _f("invoice_date", "date", "Invoice Date / تاريخ الفاتورة", True, ["Date", "Issue Date"]),
            _f("due_date", "date", "Due Date / تاريخ الاستحقاق"),
            _f("supplier_name", label="Supplier / المورد", aliases=["Vendor", "Seller", "From"]),
            _f("supplier_vat", label="Supplier VAT No / الرقم الضريبي", aliases=["VAT Number", "VAT No", "TRN"],
               regex=r"3\d{14}"),
            _f("customer_name", label="Customer / العميل", aliases=["Bill To", "Buyer"]),
            _f("subtotal", "currency", "Subtotal / المجموع الفرعي", True, ["Sub Total", "Net Amount"]),
            _f("vat_amount", "currency", "VAT / ضريبة القيمة المضافة", False, ["VAT 15%", "Tax", "VAT Amount"]),
            _f("total", "currency", "Total / الإجمالي", True, ["Total Amount", "Grand Total", "Amount Due"]),
            _f("currency", "enum", "Currency / العملة", enum=["SAR", "AED", "USD", "EUR", "GBP"]),
            _f("line_items", "table", "Line items", columns=[
                _f("description", label="Description"), _f("quantity", "number", "Qty"),
                _f("unit_price", "currency", "Unit Price"), _f("amount", "currency", "Amount")]),
        ],
        "validation": ["abs(subtotal + vat_amount - total) < 0.05"],
    },
    "purchase_order": {
        "title": "Purchase orders", "description": "Purchase orders issued to vendors.",
        "fields": [
            _f("po_number", label="PO Number / رقم أمر الشراء", required=True, aliases=["Purchase Order No", "PO No"]),
            _f("po_date", "date", "PO Date / التاريخ", True, ["Date", "Order Date"]),
            _f("vendor_name", label="Vendor / المورد", aliases=["Supplier"]),
            _f("delivery_date", "date", "Delivery Date / تاريخ التسليم"),
            _f("total", "currency", "Total / الإجمالي", True, ["Total Amount", "Grand Total"]),
            _f("line_items", "table", "Line items", columns=[
                _f("description", label="Description"), _f("quantity", "number", "Qty"),
                _f("unit_price", "currency", "Unit Price"), _f("amount", "currency", "Amount")]),
        ],
        "validation": [],
    },
    "receipt": {
        "title": "Receipts", "description": "Retail / expense receipts.",
        "fields": [
            _f("merchant_name", label="Merchant / التاجر", aliases=["Store"]),
            _f("receipt_date", "date", "Date / التاريخ", True),
            _f("vat_amount", "currency", "VAT / الضريبة", aliases=["Tax"]),
            _f("total", "currency", "Total / الإجمالي", True, ["Amount", "Total Paid"]),
            _f("payment_method", "enum", "Payment method", enum=["Cash", "Card", "Mada", "Apple Pay", "Other"]),
        ],
        "validation": [],
    },
    "bank_statement": {
        "title": "Bank statements", "description": "Account statements with balances and transactions.",
        "fields": [
            _f("bank_name", label="Bank / البنك"),
            _f("account_number", label="IBAN / رقم الآيبان", required=True, aliases=["Account No", "IBAN"],
               regex=r"SA\d{2}[A-Z0-9]{18}"),
            _f("period_from", "date", "From / من"), _f("period_to", "date", "To / إلى"),
            _f("opening_balance", "currency", "Opening Balance / الرصيد الافتتاحي", True),
            _f("closing_balance", "currency", "Closing Balance / الرصيد الختامي", True),
            _f("transactions", "table", "Transactions", columns=[
                _f("date", "date", "Date"), _f("description", label="Description"),
                _f("debit", "currency", "Debit"), _f("credit", "currency", "Credit"),
                _f("balance", "currency", "Balance")]),
        ],
        "validation": [],
    },
    "id_document": {
        "title": "ID documents", "description": "National ID / Iqama / passport data page.",
        "fields": [
            _f("full_name", label="Name / الاسم", required=True),
            _f("id_number", label="ID Number / رقم الهوية", required=True, aliases=["Iqama No", "Passport No"],
               regex=r"[12]\d{9}|[A-Z]\d{7,8}"),
            _f("nationality", label="Nationality / الجنسية"),
            _f("date_of_birth", "date", "Date of Birth / تاريخ الميلاد"),
            _f("expiry_date", "date", "Expiry Date / تاريخ الانتهاء", True),
        ],
        "validation": [],
    },
    "user_defined": {
        "title": "User-defined", "description": "Any document: define your own fields.",
        "fields": [_f("reference_number", label="Reference Number", required=True)],
        "validation": [],
    },
}


def templates() -> list[dict]:
    return [{"documentType": k, **{kk: v for kk, v in t.items()}} for k, t in TEMPLATES.items()]


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s or "instance")[:40]


# ----------------------------------------------------------------- service
class InstanceService:
    def __init__(self, svc: DocumentService):
        self.svc = svc
        self.store = svc.store
        self._lock = threading.Lock()
        self._watcher: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_poll: dict[str, float] = {}
        svc.listeners.append(self._on_event)

    # ---- CRUD ------------------------------------------------------------
    def list(self) -> list[LearningInstance]:
        return [LearningInstance.model_validate_json(b) for b in self.store.list_instance_bodies()]

    def get(self, inst_id: str) -> LearningInstance:
        body = self.store.get_instance_body(inst_id)
        if not body:
            raise InvalidInput(f"unknown learning instance '{inst_id}'")
        return LearningInstance.model_validate_json(body)

    def _save(self, inst: LearningInstance) -> LearningInstance:
        inst.updatedAt = _now()
        self.store.save_instance(inst.id, inst.name, inst.model_dump_json(), inst.status, inst.version,
                                 inst.createdAt)
        return inst

    def _validate(self, inst: LearningInstance) -> None:
        if not inst.name.strip():
            raise InvalidInput("name is required")
        if not inst.fields:
            raise InvalidInput("add at least one field")
        seen = set()
        for f in inst.fields:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", f.name or ""):
                raise InvalidInput(f"field name '{f.name}' must use letters, digits and underscore")
            if f.name in seen:
                raise InvalidInput(f"duplicate field '{f.name}'")
            seen.add(f.name)
        for t in (inst.fieldConfidence, inst.documentConfidence):
            if not 0 <= t <= 1:
                raise InvalidInput("confidence thresholds must be between 0 and 1")
        if inst.folders.inputFolder and inst.folders.inputFolder == inst.folders.outputFolder:
            raise InvalidInput("input and output folders must be different")

    def create(self, inst: LearningInstance) -> LearningInstance:
        if not inst.fields:
            t = TEMPLATES.get(inst.documentType, TEMPLATES["user_defined"])
            inst.fields = [FieldDef.model_validate(f) for f in t["fields"]]
            if not inst.validation:
                inst.validation = list(t["validation"])
        inst.id = inst.id or _slug(inst.name)
        with self._lock:
            base, n = inst.id, 2
            while self.store.get_instance_body(inst.id):
                inst.id, n = f"{base}-{n}", n + 1
            inst.status, inst.version, inst.createdAt = "draft", 1, _now()
            self._validate(inst)
            return self._save(inst)

    def update(self, inst_id: str, patch: dict[str, Any]) -> LearningInstance:
        cur = self.get(inst_id)
        protected = {"id", "status", "version", "createdAt", "updatedAt", "publishedAt"}
        data = cur.model_dump()
        for k, v in patch.items():
            if k not in protected:
                data[k] = v
        new = LearningInstance.model_validate(data)
        self._validate(new)
        if new.model_dump(exclude={"updatedAt", "learning"}) != cur.model_dump(exclude={"updatedAt", "learning"}):
            new.version = cur.version + 1
        return self._save(new)

    def publish(self, inst_id: str) -> LearningInstance:
        inst = self.get(inst_id)
        self._validate(inst)
        inst.status, inst.publishedAt = "published", _now()
        return self._save(inst)

    def unpublish(self, inst_id: str) -> LearningInstance:
        inst = self.get(inst_id)
        inst.status = "draft"
        return self._save(inst)

    def delete(self, inst_id: str) -> None:
        self.get(inst_id)
        self.store.delete_instance(inst_id)

    # ---- processing ------------------------------------------------------
    def options_for(self, inst: LearningInstance, source: str) -> ProcessOptions:
        instructions = None
        if inst.learning.enabled:
            recent = self.store.corrections(inst.id, limit=8)
            if recent:
                tips = [f"- {c['field']}: extracted {json.dumps(c['original'], ensure_ascii=False)}, "
                        f"reviewer corrected to {json.dumps(c['corrected'], ensure_ascii=False)}" for c in recent]
                instructions = "Recent reviewer corrections for this document type (learn from them):\n" + "\n".join(tips)
        return ProcessOptions(
            ocrProvider=inst.engine.ocrProvider, extractionProvider=inst.engine.extractionProvider,
            languages=inst.languages or ["en"], capabilities=inst.engine.capabilities or ["ocr_printed"],
            schema=inst.schema_for_processing(), instanceId=inst.id, instanceVersion=inst.version, source=source,
            fieldThreshold=inst.fieldConfidence, docThreshold=inst.documentConfidence, instructions=instructions)

    def upload(self, inst_id: str, data: bytes, file_name: str, source: str = "upload", wait: bool = False,
               allow_draft: bool = True) -> dict:
        inst = self.get(inst_id)
        if inst.status != "published" and not allow_draft:
            raise InvalidInput(f"learning instance '{inst_id}' is a draft - publish it before bots can use it")
        ids = self.svc.submit(data, file_name, self.options_for(inst, source), wait=wait)
        row = self.store.doc_row(ids["documentId"]) or {}
        return {**ids, "status": row.get("status"), "instanceId": inst.id, "instanceVersion": inst.version}

    def document(self, doc_id: str) -> dict:
        row = self.store.doc_row(doc_id)
        if not row:
            raise InvalidInput(f"unknown document '{doc_id}'")
        res = self.store.get_result(doc_id)
        row["fields"] = {f.name: f.value for f in res.fields} if res else {}
        row["confidence"] = {f.name: f.confidence for f in res.fields} if res else {}
        job = self.store._one("SELECT error FROM jobs WHERE document_id=? ORDER BY created_at DESC LIMIT 1", (doc_id,))
        row["error"] = json.loads(job["error"]) if job and job["error"] else None
        return row

    def validate(self, doc_id: str, fields: dict[str, Any], reviewer: str, action: str = "submit") -> dict:
        row = self.store.doc_row(doc_id)
        if not row:
            raise InvalidInput(f"unknown document '{doc_id}'")
        if row["status"] not in ("validation", "success", "validated", "invalid"):
            raise InvalidInput(f"document is '{row['status']}' and cannot be validated")
        self.svc.complete_review(doc_id, fields, reviewer, reject=(action == "invalid"))
        nxt = self.store.next_for_validation(row.get("instance_id"))
        return {**self.document(doc_id), "nextDocumentId": nxt}

    # ---- learning --------------------------------------------------------
    def _on_event(self, event: str, doc_id: str, **info) -> None:
        row = self.store.doc_row(doc_id)
        if not row or not row.get("instance_id"):
            return
        try:
            inst = self.get(row["instance_id"])
        except InvalidInput:
            return
        if event == "validated" and info.get("status") == "validated" and inst.learning.enabled:
            self._learn(inst, doc_id, info.get("corrections") or [])
        self._write_output(inst, doc_id, info.get("status") or row["status"])

    def _learn(self, inst: LearningInstance, doc_id: str, corrections: list[dict]) -> None:
        res = self.store.get_result(doc_id)
        if not res or not corrections:
            return
        changed = False
        for c in corrections:
            label = find_label_for_value(res, c["corrected"])
            if not label:
                continue
            existing = {a.lower() for a in inst.learning.learnedAliases.get(c["field"], [])}
            fdef = next((f for f in inst.fields if f.name == c["field"]), None)
            if fdef:
                existing |= {a.lower() for a in fdef.aliases} | {p.strip().lower() for p in (fdef.label or "").split("/")}
            if label.lower() not in existing:
                inst.learning.learnedAliases.setdefault(c["field"], []).append(label)
                changed = True
                log.info("instance %s learned label %r for %s", inst.id, label, c["field"])
        if changed:
            fresh = self.get(inst.id)  # keep concurrent edits, only merge learning
            fresh.learning.learnedAliases = inst.learning.learnedAliases
            self._save(fresh)

    def forget(self, inst_id: str, field: str, alias: str) -> LearningInstance:
        inst = self.get(inst_id)
        lst = inst.learning.learnedAliases.get(field, [])
        if alias in lst:
            lst.remove(alias)
        return self._save(inst)

    # ---- stats / export ----------------------------------------------------
    def stats(self, inst_id: Optional[str]) -> dict:
        return self.store.instance_stats(inst_id)

    def export(self, inst_id: str, fmt: str = "csv", statuses: Optional[list[str]] = None) -> tuple[bytes, str, str]:
        inst = self.get(inst_id)
        statuses = statuses or ["success", "validated"]
        docs = self.store.instance_documents(inst_id, statuses, limit=100000)
        scalar = [f.name for f in inst.fields if f.type != "table"]
        tables = [f for f in inst.fields if f.type == "table"]
        head = ["document_id", "file_name", "status", "processed_at", "validated_by"] + scalar
        rows, trows = [], {t.name: [] for t in tables}
        for d in docs:
            res = self.store.get_result(d["id"])
            vals = {f.name: f.value for f in res.fields} if res else {}
            rows.append([d["id"], d["file_name"], d["status"], d["updated_at"], d["validated_by"] or ""] +
                        [_cell(vals.get(n)) for n in scalar])
            for t in tables:
                for r in vals.get(t.name) or []:
                    if isinstance(r, dict):
                        trows[t.name].append([d["id"]] + [_cell(r.get(c.name)) for c in t.columns])
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        if fmt == "xlsx":
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "documents"
            ws.append(head)
            for r in rows:
                ws.append(r)
            for t in tables:
                tws = wb.create_sheet(t.name[:31])
                tws.append(["document_id"] + [c.name for c in t.columns])
                for r in trows[t.name]:
                    tws.append(r)
            buf = io.BytesIO()
            wb.save(buf)
            return (buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    f"{inst.id}-{stamp}.xlsx")
        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(head)
        w.writerows(rows)
        return ("﻿" + s.getvalue()).encode("utf-8"), "text/csv; charset=utf-8", f"{inst.id}-{stamp}.csv"

    # ---- folders -----------------------------------------------------------
    def _write_output(self, inst: LearningInstance, doc_id: str, status: str) -> None:
        out = inst.folders.outputFolder
        if not out or status not in ("success", "validation", "validated", "invalid", "failed"):
            return
        try:
            folder = Path(out) / status
            folder.mkdir(parents=True, exist_ok=True)
            row = self.store.doc_row(doc_id) or {}
            stem = Path(row.get("file_name") or doc_id).stem
            payload = {"documentId": doc_id, "instanceId": inst.id, "instanceVersion": row.get("instance_version"),
                       "status": status, "fileName": row.get("file_name"), **self.document(doc_id)}
            (folder / f"{stem}_{doc_id[:8]}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                                                          default=str), encoding="utf-8")
            f = self.store.file_bytes(doc_id)
            if f:
                (folder / f"{stem}_{doc_id[:8]}{Path(f[1]).suffix}").write_bytes(f[0])
            if status in ("validated", "invalid"):  # moved out of the validation queue
                for p in (Path(out) / "validation").glob(f"{stem}_{doc_id[:8]}*"):
                    p.unlink(missing_ok=True)
        except Exception:
            log.exception("could not write output for %s", doc_id)

    def scan_folder(self, inst: LearningInstance, allow_draft: bool = False) -> list[dict]:
        src = Path(inst.folders.inputFolder) if inst.folders.inputFolder else None
        if not src or not src.is_dir():
            return []
        done_dir = src / "processed"
        done_dir.mkdir(exist_ok=True)
        results = []
        for p in sorted(src.iterdir()):
            if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXT:
                continue
            try:
                if time.time() - p.stat().st_mtime < 2:  # still being written
                    continue
                data = p.read_bytes()
                target = done_dir / p.name
                if target.exists():
                    target = done_dir / f"{p.stem}_{uuid.uuid4().hex[:6]}{p.suffix}"
                shutil.move(str(p), str(target))
                results.append({"file": p.name, **self.upload(inst.id, data, p.name, source="folder",
                                                              allow_draft=allow_draft)})
            except Exception as e:
                log.exception("folder ingestion failed for %s", p)
                results.append({"file": p.name, "error": str(e)})
        return results

    def start_watcher(self, interval: float = 5.0) -> None:
        if self._watcher and self._watcher.is_alive():
            return

        def loop():
            while not self._stop.wait(interval):
                for inst in self.list():
                    if inst.status != "published" or not inst.folders.inputFolder:
                        continue
                    last = self._last_poll.get(inst.id, 0)
                    if time.time() - last < max(5, inst.folders.pollSeconds):
                        continue
                    self._last_poll[inst.id] = time.time()
                    try:
                        self.scan_folder(inst)
                    except Exception:
                        log.exception("watcher failed for %s", inst.id)

        self._watcher = threading.Thread(target=loop, name="docauto-folder-watcher", daemon=True)
        self._watcher.start()

    def stop_watcher(self) -> None:
        self._stop.set()


def _cell(v: Any) -> Any:
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return "" if v is None else v


def _value_variants(v: Any) -> list[str]:
    s = str(v).strip()
    out = {s}
    try:
        f = float(s.replace(",", ""))
        out |= {f"{f:,.2f}", f"{f:.2f}", f"{f:,.0f}" if f == int(f) else s, str(int(f)) if f == int(f) else s}
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = m.groups()
        out |= {f"{d}/{mo}/{y}", f"{mo}/{d}/{y}", f"{d}-{mo}-{y}", f"{d}.{mo}.{y}"}
    return [x for x in out if x]


def find_label_for_value(doc, value: Any) -> Optional[str]:
    """Find the text that labels `value` in the OCR lines: 'Label: value' or a label on the line above."""
    if value in (None, "", []) or isinstance(value, (list, dict)):
        return None
    variants = _value_variants(value)
    for p in doc.pages:
        for i, line in enumerate(p.lines):
            for v in variants:
                idx = line.text.find(v)
                if idx < 0:
                    continue
                label = line.text[:idx].strip(" :：#-\t")
                if not label and i > 0:
                    label = p.lines[i - 1].text.strip(" :：#-\t")
                if 2 <= len(label) <= 40 and re.search(r"[A-Za-z؀-ۿ]", label):
                    return label
    return None
