"""Oneoffice DocFlow: the Document Automation component of the Oneoffice RPA platform.

Concepts
--------
* Doc Type   - a reusable definition of one kind of document: fields (form + table), labels in
               English/Arabic, keywords used to recognise it, consistency checks, learned labels.
* Doc Flow   - a business process that documents travel through:
               Intake -> Classify & split -> Extract -> Rules & lookups -> Review -> Deliver.
               A flow can accept one or many Doc Types; it chooses the engine option (1/2/3),
               review thresholds and SLA, rules, and where results are delivered.
* Smart Inbox - a built-in flow that accepts ANY document: every page is classified against all
               Doc Types, mixed batches are split, and each part is extracted with its own type.
* Review Desk - human review of documents flagged by confidence, checks or rules (with SLA).
* Reference Data - lookup tables (supplier master, PO list...) used by lookup rules.

Document statuses: queued, processing, success, validation (in Review Desk), validated, invalid,
failed, split (a batch that was split into child documents).
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field

from .errors import InvalidInput
from .extraction import coerce, safe_eval
from .models import DocumentResult, ExtractedField, FieldDef, FieldSchema, ReviewStatus
from .pipeline import DocumentService, ProcessOptions

log = logging.getLogger("docauto.docflow")
SUPPORTED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
INBOX_ID = "smart-inbox"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(name: str, fallback: str = "item") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s or fallback)[:40]


def _snake(label: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower()
    return s if re.match(r"[a-z_]", s or "") else ""


# ================================================================== models
class LearnedLabels(BaseModel):
    enabled: bool = True
    aliases: dict[str, list[str]] = Field(default_factory=dict)


class DocType(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    category: str = "custom"           # invoice | purchase_order | receipt | bank_statement | id_document | custom
    languages: list[str] = Field(default_factory=lambda: ["en", "ar"])
    keywords: list[str] = Field(default_factory=list)   # words that identify this type on a page
    fields: list[FieldDef] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)     # cross-field expressions
    learning: LearnedLabels = Field(default_factory=LearnedLabels)
    version: int = 1
    createdAt: str = ""
    updatedAt: str = ""

    def schema(self) -> FieldSchema:
        learned = self.learning.aliases if self.learning.enabled else {}
        fields = []
        for f in self.fields:
            f2 = f.model_copy(deep=True)
            f2.aliases += [a for a in learned.get(f.name, []) if a not in f2.aliases]
            fields.append(f2)
        return FieldSchema(id=f"doctype:{self.id}", name=self.name, description=self.description,
                           documentLanguages=self.languages, fields=fields, validation=self.checks)

    def recognition_terms(self) -> list[tuple[str, float]]:
        """Weighted terms used to recognise this type on a page."""
        terms: list[tuple[str, float]] = [(k, 3.0) for k in self.keywords]
        terms += [(w, 1.5) for w in re.split(r"[\s/_-]+", self.name) if len(w) > 3]
        for f in self.fields:
            for part in (f.label or "").split("/"):
                if part.strip():
                    terms.append((part.strip(), 2.0 if f.required else 1.0))
            terms += [(a, 0.8) for a in f.aliases]
        return terms


class EngineChoice(BaseModel):
    option: Literal["tier1_opensource", "tier2_cloud", "tier3_ai"] = "tier1_opensource"
    ocrProvider: str = "auto"
    extractionProvider: str = "auto"
    capabilities: list[str] = Field(default_factory=lambda: ["ocr_printed"])


class ClassifySettings(BaseModel):
    mode: Literal["single", "auto"] = "single"   # single: every document is the flow's (first) doc type
    split: bool = False                          # split mixed batches into one document per part
    minScore: float = 2.0                        # below this a page is 'unclassified' -> Review Desk


class ReviewSettings(BaseModel):
    fieldConfidence: float = 0.85
    documentConfidence: float = 0.80
    slaHours: float = 24
    slaWebhook: str = ""


class Rule(BaseModel):
    id: str = ""
    type: Literal["duplicate", "lookup", "ksa_vat", "threshold", "required", "expression"]
    name: str = ""
    enabled: bool = True
    action: Literal["review", "reject", "warn"] = "review"
    docTypes: list[str] = Field(default_factory=list)   # empty = all doc types of the flow
    params: dict[str, Any] = Field(default_factory=dict)


class Delivery(BaseModel):
    id: str = ""
    type: Literal["webhook", "folder", "excel", "start_bot"]
    name: str = ""
    enabled: bool = True
    on: list[str] = Field(default_factory=lambda: ["success", "validated"])
    params: dict[str, Any] = Field(default_factory=dict)


class Intake(BaseModel):
    inputFolder: str = ""
    pollSeconds: int = 30


class Flow(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    folder: str = ""                                    # where it sits in the platform, e.g. "Finance / Accounts payable"
    docTypes: list[str] = Field(default_factory=list)   # Smart Inbox: empty = all doc types
    classify: ClassifySettings = Field(default_factory=ClassifySettings)
    engine: EngineChoice = Field(default_factory=EngineChoice)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    rules: list[Rule] = Field(default_factory=list)
    deliveries: list[Delivery] = Field(default_factory=list)
    intake: Intake = Field(default_factory=Intake)
    status: Literal["draft", "live"] = "draft"
    isInbox: bool = False
    version: int = 1
    createdAt: str = ""
    updatedAt: str = ""


# ================================================================== doc type library
def _f(name, type_="string", label=None, required=False, aliases=None, regex=None, fmt=None, enum=None, columns=None):
    d = {"name": name, "type": type_, "label": label, "required": required, "aliases": aliases or []}
    if regex:
        d["regex"] = regex
    if fmt:
        d["format"] = fmt
    if enum:
        d["enumValues"] = enum
    if columns:
        d["columns"] = columns
    return d


LINE_ITEMS = _f("line_items", "table", "Line items", columns=[
    _f("description", label="Description"), _f("quantity", "number", "Qty"),
    _f("unit_price", "currency", "Unit Price"), _f("amount", "currency", "Amount")])

LIBRARY: dict[str, dict[str, Any]] = {
    "invoice": {"name": "Invoice", "description": "Supplier / tax invoices (VAT), English and Arabic.",
                "keywords": ["invoice", "tax invoice", "فاتورة", "فاتورة ضريبية", "VAT"],
                "fields": [
                    _f("invoice_number", label="Invoice Number / رقم الفاتورة", required=True, aliases=["Invoice No", "Bill No"]),
                    _f("invoice_date", "date", "Invoice Date / تاريخ الفاتورة", True, ["Date", "Issue Date"]),
                    _f("due_date", "date", "Due Date / تاريخ الاستحقاق"),
                    _f("supplier_name", label="Supplier / المورد", aliases=["Vendor", "Seller"]),
                    _f("supplier_vat", label="Supplier VAT No / الرقم الضريبي", aliases=["VAT Number", "VAT No", "TRN"],
                       regex=r"3\d{14}"),
                    _f("customer_name", label="Customer / العميل", aliases=["Bill To", "Buyer"]),
                    _f("po_number", label="PO Number / رقم أمر الشراء", aliases=["Purchase Order", "PO No"]),
                    _f("subtotal", "currency", "Subtotal / المجموع الفرعي", True, ["Sub Total", "Net Amount"]),
                    _f("vat_amount", "currency", "VAT / ضريبة القيمة المضافة", False, ["VAT 15%", "Tax", "VAT Amount"]),
                    _f("total", "currency", "Total / الإجمالي", True, ["Total Amount", "Grand Total", "Amount Due"]),
                    _f("currency", "enum", "Currency / العملة", enum=["SAR", "AED", "USD", "EUR", "GBP"]), LINE_ITEMS],
                "checks": ["abs(subtotal + vat_amount - total) < 0.05"]},
    "purchase_order": {"name": "Purchase order", "description": "Purchase orders issued to vendors.",
                       "keywords": ["purchase order", "P.O.", "أمر شراء"],
                       "fields": [
                           _f("po_number", label="PO Number / رقم أمر الشراء", required=True, aliases=["Purchase Order No", "PO No"]),
                           _f("po_date", "date", "PO Date / التاريخ", True, ["Date", "Order Date"]),
                           _f("vendor_name", label="Vendor / المورد", aliases=["Supplier"]),
                           _f("delivery_date", "date", "Delivery Date / تاريخ التسليم"),
                           _f("total", "currency", "Total / الإجمالي", True, ["Total Amount", "Grand Total"]), LINE_ITEMS],
                       "checks": []},
    "receipt": {"name": "Receipt", "description": "Retail / expense receipts.",
                "keywords": ["receipt", "إيصال", "cashier", "thank you"],
                "fields": [_f("merchant_name", label="Merchant / التاجر", aliases=["Store"]),
                           _f("receipt_date", "date", "Date / التاريخ", True),
                           _f("vat_amount", "currency", "VAT / الضريبة", aliases=["Tax"]),
                           _f("total", "currency", "Total / الإجمالي", True, ["Amount", "Total Paid"]),
                           _f("payment_method", "enum", "Payment method", enum=["Cash", "Card", "Mada", "Apple Pay", "Other"])],
                "checks": []},
    "bank_statement": {"name": "Bank statement", "description": "Account statements with balances and transactions.",
                       "keywords": ["statement", "IBAN", "opening balance", "كشف حساب"],
                       "fields": [_f("bank_name", label="Bank / البنك"),
                                  _f("account_number", label="IBAN / رقم الآيبان", required=True, aliases=["Account No", "IBAN"],
                                     regex=r"SA\d{2}[A-Z0-9]{18}"),
                                  _f("period_from", "date", "From / من"), _f("period_to", "date", "To / إلى"),
                                  _f("opening_balance", "currency", "Opening Balance / الرصيد الافتتاحي", True),
                                  _f("closing_balance", "currency", "Closing Balance / الرصيد الختامي", True),
                                  _f("transactions", "table", "Transactions", columns=[
                                      _f("date", "date", "Date"), _f("description", label="Description"),
                                      _f("debit", "currency", "Debit"), _f("credit", "currency", "Credit"),
                                      _f("balance", "currency", "Balance")])],
                       "checks": []},
    "id_document": {"name": "ID document", "description": "National ID / Iqama / passport data page.",
                    "keywords": ["identity", "iqama", "passport", "هوية", "جواز", "nationality"],
                    "fields": [_f("full_name", label="Name / الاسم", required=True),
                               _f("id_number", label="ID Number / رقم الهوية", required=True, aliases=["Iqama No", "Passport No"],
                                  regex=r"[12]\d{9}|[A-Z]\d{7,8}"),
                               _f("nationality", label="Nationality / الجنسية"),
                               _f("date_of_birth", "date", "Date of Birth / تاريخ الميلاد"),
                               _f("expiry_date", "date", "Expiry Date / تاريخ الانتهاء", True)],
                    "checks": []},
}


def library() -> list[dict]:
    return [{"category": k, **v} for k, v in LIBRARY.items()]


# ================================================================== rules
def run_rules(store, flow: Flow, doctype: Optional[DocType], doc_id: str, result: DocumentResult,
              refs: "RefData") -> list[dict]:
    """Return flags: {rule, type, action, message, field?}. May enrich result fields (lookups)."""
    flags: list[dict] = []
    vals = {f.name: f.value for f in result.fields}
    by = {f.name: f for f in result.fields}

    def flag(rule: Rule, message: str, field: Optional[str] = None):
        flags.append({"rule": rule.name or rule.type, "type": rule.type, "action": rule.action,
                      "message": message, "field": field})
        if field and field in by and rule.action != "warn":
            by[field].validation.passed = False
            if message not in by[field].validation.errors:
                by[field].validation.errors.append(message)

    for r in flow.rules:
        if not r.enabled or (r.docTypes and (not doctype or doctype.id not in r.docTypes)):
            continue
        p = r.params
        try:
            if r.type == "duplicate":
                keys = [k for k in p.get("fields", []) if k]
                if keys and all(vals.get(k) not in (None, "") for k in keys):
                    key = "|".join(str(vals[k]).strip().lower() for k in keys)
                    other = store.dedupe_find(flow.id, key, doc_id)
                    if other:
                        flag(r, f"Possible duplicate of document {other[:8]} (same {', '.join(keys)})", keys[0])
                    store.dedupe_add(flow.id, key, doc_id)
            elif r.type == "lookup":
                field, table, col = p.get("field"), p.get("table"), p.get("column")
                v = vals.get(field)
                if field and table and col and v not in (None, ""):
                    row = refs.find(table, col, v)
                    if row is None:
                        flag(r, p.get("message") or f"{field} '{v}' not found in {refs.name(table)}", field)
                    else:
                        for target, src in (p.get("enrich") or {}).items():
                            if src in row and row[src] not in (None, ""):
                                if target in by:
                                    if by[target].value in (None, ""):
                                        by[target].value, by[target].confidence = row[src], 1.0
                                        by[target].providerId = f"lookup:{table}"
                                else:
                                    nf = ExtractedField(name=target, value=row[src], confidence=1.0,
                                                        providerId=f"lookup:{table}")
                                    result.fields.append(nf)
                                    by[target] = nf
            elif r.type == "ksa_vat":
                vf = p.get("vatNumberField", "supplier_vat")
                sub, vat, tot = p.get("subtotalField", "subtotal"), p.get("vatField", "vat_amount"), p.get("totalField", "total")
                rate = float(p.get("rate", 0.15))
                vn = str(vals.get(vf) or "")
                if vn and not re.fullmatch(r"3\d{13}3", vn):
                    flag(r, "VAT number must be 15 digits starting and ending with 3", vf)
                if isinstance(vals.get(sub), (int, float)) and isinstance(vals.get(vat), (int, float)):
                    if abs(round(vals[sub] * rate, 2) - vals[vat]) > max(0.05, vals[sub] * 0.001):
                        flag(r, f"VAT {vals[vat]} is not {rate:.0%} of subtotal {vals[sub]}", vat)
                if all(isinstance(vals.get(k), (int, float)) for k in (sub, vat, tot)):
                    if abs(vals[sub] + vals[vat] - vals[tot]) > 0.05:
                        flag(r, "Total does not equal subtotal + VAT", tot)
            elif r.type == "threshold":
                field, op, limit = p.get("field"), p.get("op", ">"), float(p.get("value", 0))
                v = vals.get(field)
                if isinstance(v, (int, float)):
                    hit = {">": v > limit, ">=": v >= limit, "<": v < limit, "<=": v <= limit, "==": v == limit}.get(op, False)
                    if hit:
                        flag(r, p.get("message") or f"{field} {v:,.2f} {op} {limit:,.2f}", field)
            elif r.type == "required":
                for field in p.get("fields", []):
                    if vals.get(field) in (None, "", []):
                        flag(r, f"{field} is required", field)
            elif r.type == "expression":
                expr = p.get("expression", "")
                if expr and not safe_eval(expr, vals):
                    flag(r, p.get("message") or f"Check failed: {expr}")
        except Exception as e:  # a broken rule must not break processing
            flag(r, f"Rule could not run: {type(e).__name__}: {e}")
            flags[-1]["action"] = "review"
    return flags


class RefData:
    def __init__(self, store):
        self.store = store
        self._cache: dict[str, dict] = {}

    def table(self, ref_id: str) -> Optional[dict]:
        if ref_id not in self._cache:
            self._cache[ref_id] = self.store.ref_get(ref_id)
        return self._cache[ref_id]

    def name(self, ref_id: str) -> str:
        t = self.table(ref_id)
        return t["name"] if t else ref_id

    def find(self, ref_id: str, column: str, value: Any) -> Optional[dict]:
        t = self.table(ref_id)
        if not t:
            return None
        want = re.sub(r"\s+", " ", str(value)).strip().lower()
        for row in t["rows"]:
            if re.sub(r"\s+", " ", str(row.get(column, ""))).strip().lower() == want:
                return row
        return None


def parse_table_file(data: bytes, file_name: str) -> tuple[list[str], list[dict]]:
    if file_name.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows = [[("" if c is None else str(c)).strip() for c in r] for r in ws.iter_rows(values_only=True)]
    else:
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t") if text.strip() else csv.excel
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]
    rows = [r for r in rows if any(r)]
    if not rows:
        raise InvalidInput("the file is empty")
    head = [h or f"col{i + 1}" for i, h in enumerate(rows[0])]
    return head, [dict(zip(head, r)) for r in rows[1:]]


# ================================================================== classification & split
def classify_page(text: str, doctypes: list[DocType]) -> tuple[Optional[str], float, dict[str, float]]:
    low = text.lower()
    scores = {}
    for dt in doctypes:
        s = 0.0
        for term, w in dt.recognition_terms():
            t = term.lower().strip()
            if len(t) >= 2 and re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", low):
                s += w
        scores[dt.id] = round(s, 2)
    if not scores:
        return None, 0.0, {}
    best = max(scores, key=scores.get)
    return best, scores[best], scores


def start_anchor(dt: DocType) -> Optional[str]:
    req = next((f for f in dt.fields if f.required and f.type != "table"), None)
    if not req or not req.label:
        return None
    return req.label.split("/")[0].strip().lower()


def split_pages(pages_text: list[str], doctypes: list[DocType], min_score: float, split: bool) -> list[dict]:
    """Group pages into documents: [{doctype, pages:[1..], score}]. doctype None = unclassified."""
    by_id = {d.id: d for d in doctypes}
    segs: list[dict] = []
    for i, txt in enumerate(pages_text, 1):
        dt_id, score, _ = classify_page(txt, doctypes)
        if score < min_score:
            dt_id = None
        anchor = start_anchor(by_id[dt_id]) if dt_id else None
        starts_new = bool(anchor and anchor in txt.lower())
        cur = segs[-1] if segs else None
        if not split:
            if cur is None:
                segs.append({"doctype": dt_id, "pages": [i], "score": score})
            else:
                cur["pages"].append(i)
                if score > cur["score"]:
                    cur["doctype"], cur["score"] = dt_id, score
            continue
        if cur and (dt_id == cur["doctype"] or (dt_id is None and cur["doctype"] is not None and not starts_new)) \
                and not (starts_new and cur.get("anchored")):
            cur["pages"].append(i)
            cur["anchored"] = cur.get("anchored") or starts_new
        else:
            segs.append({"doctype": dt_id, "pages": [i], "score": score, "anchored": starts_new})
    for s in segs:
        s.pop("anchored", None)
    return segs


def extract_pages(data: bytes, mime: str, pages: list[int]) -> tuple[bytes, str]:
    """Return a new file containing only `pages` (1-based) of the original."""
    if mime == "application/pdf":
        import pypdfium2 as pdfium
        src = pdfium.PdfDocument(data)
        out = pdfium.PdfDocument.new()
        out.import_pages(src, [p - 1 for p in pages])
        buf = io.BytesIO()
        out.save(buf)
        return buf.getvalue(), ".pdf"
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    frames = []
    for p in pages:
        img.seek(p - 1)
        frames.append(img.copy().convert("RGB"))
    buf = io.BytesIO()
    if len(frames) == 1:
        frames[0].save(buf, format="PNG")
        return buf.getvalue(), ".png"
    frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
    return buf.getvalue(), ".tif"


# ================================================================== teach from samples
_LABEL_VALUE = re.compile(r"^\s*([A-Za-z؀-ۿ][A-Za-z؀-ۿ0-9 .%#/()'&-]{1,38}?)\s*[:：]\s*(.+?)\s*$")


def infer_type(values: list[str]) -> str:
    vals = [v for v in values if v]
    if not vals:
        return "string"
    if all(coerce(v, "date")[1] and re.search(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", v) for v in vals):
        return "date"
    cur = r"(?:SAR|USD|AED|EUR|GBP|\$|€|£|ر\.س)"
    if all(re.fullmatch(cur + r"?\s*-?\d[\d,]*(\.\d+)?\s*" + cur + "?", v.strip()) for v in vals):
        has_dec = any(re.search(r"[.,]\d{2}\b", v) for v in vals)
        return "currency" if has_dec or any(re.search(r"SAR|USD|AED|EUR|ر\.س", v) for v in vals) else "number"
    return "string"


def suggest_from_results(results: list[DocumentResult]) -> dict:
    """Heuristic proposal of a Doc Type from OCR results of 1-3 samples (no AI needed)."""
    found: dict[str, dict] = {}
    texts = []
    for r in results:
        texts.append(r.text)
        for p in r.pages:
            for line in p.lines:
                m = _LABEL_VALUE.match(line.text)
                if not m:
                    continue
                label, value = m.group(1).strip(), m.group(2).strip()
                if re.fullmatch(r"[\d\s.,/-]+", label):
                    continue
                key = label.lower()
                e = found.setdefault(key, {"label": label, "values": [], "count": 0})
                e["values"].append(value)
                e["count"] += 1
    # category guess
    best_cat, best_score = "custom", 0.0
    joined = "\n".join(texts)
    for cat, lib in LIBRARY.items():
        dt = DocType(name=lib["name"], keywords=lib["keywords"], fields=[FieldDef.model_validate(f) for f in lib["fields"]])
        _, s, _ = classify_page(joined, [dt])
        if s > best_score:
            best_cat, best_score = cat, s
    fields, used = [], set()
    for e in sorted(found.values(), key=lambda x: -x["count"]):
        name = _snake(e["label"]) or f"field_{len(fields) + 1}"
        if name in used:
            continue
        used.add(name)
        fields.append({"name": name, "type": infer_type(e["values"]), "label": e["label"], "aliases": [],
                       "required": e["count"] >= len(results), "samples": e["values"][:3], "source": "found on samples"})
    # merge with library template when the category is recognised
    if best_cat != "custom" and best_score >= 3:
        for f in LIBRARY[best_cat]["fields"]:
            labels = [p.strip().lower() for p in (f.get("label") or "").split("/")] + [a.lower() for a in f.get("aliases", [])]
            match = next((x for x in fields if x["label"].lower() in labels), None)
            if match:
                match.update({"name": f["name"], "type": f["type"], "label": f["label"], "aliases": f.get("aliases", []),
                              **({"regex": f["regex"]} if f.get("regex") else {}),
                              **({"columns": f["columns"]} if f.get("columns") else {})})
                match["source"] = "found on samples · matched template"
            elif f["name"] not in used:
                fields.append({**f, "samples": [], "source": f"{LIBRARY[best_cat]['name']} template (not seen on samples)",
                               "suggestOff": True})
                used.add(f["name"])
    langs = sorted({l.language for r in results for p in r.pages for l in p.lines if l.language})
    # title lines (short, no 'label: value') near the top identify the document type
    titles = []
    for r in results[:1]:
        for line in [l for p in r.pages[:1] for l in p.lines][:4]:
            t = line.text.strip()
            if 3 <= len(t) <= 40 and ":" not in t and not re.search(r"\d", t):
                titles.append(t)
    return {"category": best_cat, "categoryScore": best_score,
            "name": LIBRARY[best_cat]["name"] if best_cat != "custom" else "New document type",
            "languages": langs or ["en"], "keywords": LIBRARY.get(best_cat, {}).get("keywords", []) or titles[1:3] or titles[:1],
            "checks": LIBRARY.get(best_cat, {}).get("checks", []), "fields": fields, "method": "rules"}


# ================================================================== service
class DocFlowService:
    def __init__(self, svc: DocumentService):
        self.svc = svc
        self.store = svc.store
        self._lock = threading.Lock()
        self._xlsx_lock = threading.Lock()
        self._stop = threading.Event()
        self._watcher: Optional[threading.Thread] = None
        self._last_poll: dict[str, float] = {}
        svc.post_processors.append(self._post_process)
        svc.listeners.append(self._on_event)
        self._seed()

    # ---------------------------------------------------------------- setup
    def _seed(self) -> None:
        if not self.store.df_list("doctype"):
            for cat, lib in LIBRARY.items():
                self.save_doctype(DocType(id=cat, name=lib["name"], description=lib["description"], category=cat,
                                          keywords=lib["keywords"], checks=lib["checks"],
                                          fields=[FieldDef.model_validate(f) for f in lib["fields"]]), new=True)
        raw = self.store.df_get("flow", INBOX_ID)
        if raw:
            inbox = Flow.model_validate_json(raw)
            if inbox.name == "Smart Inbox":  # brand rename (v3.1)
                inbox.name = "One Inbox"
                self.store.df_save("flow", INBOX_ID, inbox.name, inbox.model_dump_json(), inbox.status)
        else:
            self.save_flow(Flow(id=INBOX_ID, name="One Inbox", isInbox=True, status="live",
                                description="Drop any document: it is recognised, split and extracted automatically.",
                                classify=ClassifySettings(mode="auto", split=True)), new=True)

    # ---------------------------------------------------------------- doc types
    def doctypes(self) -> list[DocType]:
        return [DocType.model_validate_json(b) for b in self.store.df_list("doctype")]

    def doctype(self, dt_id: str) -> DocType:
        b = self.store.df_get("doctype", dt_id)
        if not b:
            raise InvalidInput(f"unknown document type '{dt_id}'")
        return DocType.model_validate_json(b)

    def _check_fields(self, fields: list[FieldDef]) -> None:
        if not fields:
            raise InvalidInput("add at least one field")
        seen = set()
        for f in fields:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", f.name or ""):
                raise InvalidInput(f"field name '{f.name}' must use letters, digits and underscore")
            if f.name in seen:
                raise InvalidInput(f"duplicate field '{f.name}'")
            seen.add(f.name)

    def save_doctype(self, dt: DocType, new: bool = False) -> DocType:
        if not dt.name.strip():
            raise InvalidInput("name is required")
        self._check_fields(dt.fields)
        with self._lock:
            if new:
                base = dt.id or _slug(dt.name, "doctype")
                dt.id, n = base, 2
                while self.store.df_get("doctype", dt.id):
                    dt.id, n = f"{base}-{n}", n + 1
                dt.createdAt, dt.version = _now(), 1
            dt.updatedAt = _now()
            self.store.df_save("doctype", dt.id, dt.name, dt.model_dump_json())
        return dt

    def update_doctype(self, dt_id: str, patch: dict) -> DocType:
        cur = self.doctype(dt_id)
        data = cur.model_dump()
        data.update({k: v for k, v in patch.items() if k not in ("id", "createdAt", "updatedAt", "version")})
        new = DocType.model_validate(data)
        if new.model_dump(exclude={"updatedAt", "learning"}) != cur.model_dump(exclude={"updatedAt", "learning"}):
            new.version = cur.version + 1
        return self.save_doctype(new)

    def delete_doctype(self, dt_id: str) -> None:
        self.doctype(dt_id)
        users = [f.name for f in self.flows() if dt_id in f.docTypes]
        if users:
            raise InvalidInput(f"used by flow(s): {', '.join(users)}")
        self.store.df_delete("doctype", dt_id)

    # ---------------------------------------------------------------- flows
    def flows(self) -> list[Flow]:
        out = [Flow.model_validate_json(b) for b in self.store.df_list("flow")]
        return sorted(out, key=lambda f: (not f.isInbox, f.name.lower()))

    def flow(self, flow_id: str) -> Flow:
        b = self.store.df_get("flow", flow_id)
        if not b:
            raise InvalidInput(f"unknown flow '{flow_id}'")
        return Flow.model_validate_json(b)

    def _check_flow(self, fl: Flow) -> None:
        if not fl.name.strip():
            raise InvalidInput("name is required")
        if not fl.isInbox and not fl.docTypes:
            raise InvalidInput("choose at least one document type")
        for d in fl.docTypes:
            self.doctype(d)
        for t in (fl.review.fieldConfidence, fl.review.documentConfidence):
            if not 0 <= t <= 1:
                raise InvalidInput("confidence thresholds must be between 0 and 1")
        for i, r in enumerate(fl.rules):
            r.id = r.id or f"r{i + 1}"
            if r.type == "lookup" and r.params.get("table") and not self.store.ref_get(r.params["table"]):
                raise InvalidInput(f"rule '{r.name or r.type}': reference table '{r.params['table']}' not found")
        for i, d in enumerate(fl.deliveries):
            d.id = d.id or f"d{i + 1}"
            if d.type in ("webhook", "start_bot") and not str(d.params.get("url", "")).startswith(("http://", "https://")):
                raise InvalidInput(f"delivery '{d.name or d.type}': URL must start with http:// or https://")
            if d.type in ("folder", "excel") and not d.params.get("path"):
                raise InvalidInput(f"delivery '{d.name or d.type}': path is required")

    def save_flow(self, fl: Flow, new: bool = False) -> Flow:
        self._check_flow(fl)
        with self._lock:
            if new:
                base = fl.id or _slug(fl.name, "flow")
                fl.id, n = base, 2
                while self.store.df_get("flow", fl.id):
                    fl.id, n = f"{base}-{n}", n + 1
                fl.createdAt, fl.version = _now(), 1
            fl.updatedAt = _now()
            self.store.df_save("flow", fl.id, fl.name, fl.model_dump_json(), fl.status)
        return fl

    def update_flow(self, flow_id: str, patch: dict) -> Flow:
        cur = self.flow(flow_id)
        data = cur.model_dump()
        data.update({k: v for k, v in patch.items() if k not in ("id", "isInbox", "createdAt", "updatedAt", "version")})
        new = Flow.model_validate(data)
        if new.model_dump(exclude={"updatedAt"}) != cur.model_dump(exclude={"updatedAt"}):
            new.version = cur.version + 1
        return self.save_flow(new)

    def delete_flow(self, flow_id: str) -> None:
        fl = self.flow(flow_id)
        if fl.isInbox:
            raise InvalidInput("the Smart Inbox cannot be deleted")
        self.store.df_delete("flow", flow_id)

    def flow_doctypes(self, fl: Flow) -> list[DocType]:
        return self.doctypes() if (fl.isInbox and not fl.docTypes) else [self.doctype(d) for d in fl.docTypes]

    # ---------------------------------------------------------------- intake
    def _options(self, fl: Flow, dt: Optional[DocType], source: str, **extra) -> ProcessOptions:
        instructions = None
        if dt and dt.learning.enabled:
            recent = self.store.corrections(dt.id, limit=8)
            if recent:
                instructions = "Recent reviewer corrections for this document type (learn from them):\n" + "\n".join(
                    f"- {c['field']}: extracted {json.dumps(c['original'], ensure_ascii=False)}, corrected to "
                    f"{json.dumps(c['corrected'], ensure_ascii=False)}" for c in recent)
        return ProcessOptions(
            ocrProvider=fl.engine.ocrProvider, extractionProvider=fl.engine.extractionProvider,
            languages=(dt.languages if dt else None) or ["en", "ar"], capabilities=fl.engine.capabilities or ["ocr_printed"],
            schema=dt.schema() if dt else None, instanceId=fl.id, instanceVersion=fl.version, source=source,
            doctypeId=dt.id if dt else None, fieldThreshold=fl.review.fieldConfidence,
            docThreshold=fl.review.documentConfidence, instructions=instructions, **extra)

    def upload(self, flow_id: str, data: bytes, file_name: str, source: str = "upload", wait: bool = False,
               require_live: bool = False) -> dict:
        fl = self.flow(flow_id)
        if require_live and fl.status != "live":
            raise InvalidInput(f"flow '{fl.name}' is a draft - set it live before bots or folders use it")
        types = self.flow_doctypes(fl)
        if not types:
            raise InvalidInput("no document types available - create one first")
        if fl.classify.mode == "single" or (len(types) == 1 and not fl.classify.split):
            ids = self.svc.submit(data, file_name, self._options(fl, types[0], source), wait=wait)
        else:
            opt = self._options(fl, None, source, role="batch", waitChildren=wait)
            ids = self.svc.submit(data, file_name, opt, wait=wait)
        return {**ids, **self.document(ids["documentId"], brief=True)}

    # ---------------------------------------------------------------- post-processing (inside the job)
    def _post_process(self, doc_id: str, result: DocumentResult, opt: ProcessOptions, status: str) -> Optional[str]:
        if not opt.instanceId:
            return None
        try:
            fl = self.flow(opt.instanceId)
        except InvalidInput:
            return None
        if opt.role == "batch":
            return self._split(fl, doc_id, result, opt)
        dt = None
        if opt.doctypeId:
            try:
                dt = self.doctype(opt.doctypeId)
            except InvalidInput:
                dt = None
        if dt is None:
            result.review.required, result.review.status = True, ReviewStatus.pending
            result.warnings.append("Unclassified: choose the document type in One Desk")
            self.store.save_result(result)
            self.store.set_doc_fields(doc_id, flags=json.dumps([{"rule": "classification", "type": "classify",
                                                                 "action": "review", "message": "Document type not recognised"}]))
            return "validation"
        flags = run_rules(self.store, fl, dt, doc_id, result, RefData(self.store))
        self.store.set_doc_fields(doc_id, flags=json.dumps(flags, ensure_ascii=False))
        new = status
        if any(f["action"] == "reject" for f in flags):
            new = "invalid"
        elif any(f["action"] == "review" for f in flags) and status == "success":
            new = "validation"
        if flags:
            result.warnings.extend(f"{f['rule']}: {f['message']}" for f in flags if f["action"] != "warn")
            if new == "validation":
                result.review.required, result.review.status = True, ReviewStatus.pending
        self.store.save_result(result)  # also persists lookup enrichment
        return new

    def _split(self, fl: Flow, doc_id: str, result: DocumentResult, opt: ProcessOptions) -> str:
        types = self.flow_doctypes(fl)
        texts = ["\n".join(l.text for l in p.lines) for p in result.pages]
        segs = split_pages(texts, types, fl.classify.minScore, fl.classify.split)
        data, file_name, mime = self.store.file_bytes(doc_id)
        stem, by_id = Path(file_name).stem, {d.id: d for d in types}
        for i, s in enumerate(segs, 1):
            dt = by_id.get(s["doctype"]) if s["doctype"] else None
            if len(segs) == 1:
                part, ext = data, Path(file_name).suffix
            else:
                part, ext = extract_pages(data, mime, s["pages"])
            label = (dt.name if dt else "unclassified").lower().replace(" ", "-")
            name = f"{stem}_{i}-{label}{ext}" if len(segs) > 1 else file_name
            child_opt = self._options(fl, dt, opt.source, parentId=doc_id, reuseFrom=doc_id, reusePages=s["pages"])
            self.svc.submit(part, name, child_opt, wait=opt.waitChildren)
        result.warnings.append(f"Split into {len(segs)} document(s): " + ", ".join(
            f"pages {s['pages'][0]}-{s['pages'][-1]} → {by_id[s['doctype']].name if s['doctype'] else 'unclassified'}"
            for s in segs))
        self.store.save_result(result)
        return "split"

    # ---------------------------------------------------------------- documents & review
    def document(self, doc_id: str, brief: bool = False) -> dict:
        row = self.store.doc_row(doc_id)
        if not row:
            raise InvalidInput(f"unknown document '{doc_id}'")
        row["flowId"] = row.pop("instance_id")
        row["flags"] = json.loads(row["flags"]) if row.get("flags") else []
        if brief:
            return {"status": row["status"], "flowId": row["flowId"], "doctypeId": row["doctype_id"]}
        res = self.store.get_result(doc_id)
        row["fields"] = {f.name: f.value for f in res.fields} if res else {}
        row["confidence"] = {f.name: f.confidence for f in res.fields} if res else {}
        row["children"] = self.store.children(doc_id) if row["status"] == "split" else []
        job = self.store._one("SELECT error FROM jobs WHERE document_id=? ORDER BY created_at DESC LIMIT 1", (doc_id,))
        row["error"] = json.loads(job["error"]) if job and job["error"] else None
        return row

    def review(self, doc_id: str, fields: dict, reviewer: str, action: str = "submit") -> dict:
        row = self.store.doc_row(doc_id)
        if not row:
            raise InvalidInput(f"unknown document '{doc_id}'")
        if row["status"] not in ("validation", "success", "validated", "invalid"):
            raise InvalidInput(f"document is '{row['status']}' and cannot be reviewed")
        self.svc.complete_review(doc_id, fields, reviewer, reject=(action == "invalid"))
        return {**self.document(doc_id), "nextDocumentId": self.store.next_for_validation(row.get("instance_id"))}

    def reclassify(self, doc_id: str, dt_id: str) -> dict:
        row = self.store.doc_row(doc_id)
        if not row:
            raise InvalidInput(f"unknown document '{doc_id}'")
        dt, fl = self.doctype(dt_id), self.flow(row["instance_id"])
        data, file_name, _ = self.store.file_bytes(doc_id)
        self.store.set_doc_status(doc_id, "invalid", validated_by="reclassified")
        opt = self._options(fl, dt, row.get("source") or "upload", parentId=row.get("parent_id"), reuseFrom=doc_id)
        ids = self.svc.submit(data, file_name, opt, wait=True)
        return self.document(ids["documentId"])

    def review_queue(self, flow_id: Optional[str]) -> list[dict]:
        flows = {f.id: f for f in self.flows()}
        dts = {d.id: d.name for d in self.doctypes()}
        out = []
        now = datetime.now(timezone.utc)
        for d in self.store.instance_documents(flow_id, ["validation"], limit=1000):
            fl = flows.get(d["instance_id"])
            age_h = (now - datetime.fromisoformat(d["created_at"])).total_seconds() / 3600
            sla = fl.review.slaHours if fl else 24
            out.append({**d, "flowId": d["instance_id"], "flowName": fl.name if fl else d["instance_id"],
                        "doctypeName": dts.get(d["doctype_id"], "Unclassified"), "ageHours": round(age_h, 2),
                        "slaHours": sla, "overdue": age_h > sla,
                        "flags": json.loads(d["flags"]) if d.get("flags") else []})
        return sorted(out, key=lambda x: (not x["overdue"], x["created_at"]))

    # ---------------------------------------------------------------- events: learning + delivery
    def _on_event(self, event: str, doc_id: str, **info) -> None:
        row = self.store.doc_row(doc_id)
        if not row or not row.get("instance_id"):
            return
        status = info.get("status") or row["status"]
        if event == "validated" and status == "validated" and row.get("doctype_id"):
            self._learn(row["doctype_id"], doc_id, info.get("corrections") or [])
        if status in ("success", "validated", "invalid", "failed", "validation"):
            try:
                fl = self.flow(row["instance_id"])
            except InvalidInput:
                return
            threading.Thread(target=self._deliver, args=(fl, doc_id, status), daemon=True).start()

    def _learn(self, dt_id: str, doc_id: str, corrections: list[dict]) -> None:
        from .instances import find_label_for_value  # shared helper
        try:
            dt = self.doctype(dt_id)
        except InvalidInput:
            return
        if not dt.learning.enabled or not corrections:
            return
        res = self.store.get_result(doc_id)
        if not res:
            return
        changed = False
        for c in corrections:
            label = find_label_for_value(res, c["corrected"])
            if not label:
                continue
            fdef = next((f for f in dt.fields if f.name == c["field"]), None)
            known = {a.lower() for a in dt.learning.aliases.get(c["field"], [])}
            if fdef:
                known |= {a.lower() for a in fdef.aliases} | {p.strip().lower() for p in (fdef.label or "").split("/")}
            if label.lower() not in known:
                dt.learning.aliases.setdefault(c["field"], []).append(label)
                changed = True
        if changed:
            fresh = self.doctype(dt_id)
            fresh.learning.aliases = dt.learning.aliases
            self.save_doctype(fresh)

    def forget(self, dt_id: str, field: str, alias: str) -> LearnedLabels:
        dt = self.doctype(dt_id)
        if alias in dt.learning.aliases.get(field, []):
            dt.learning.aliases[field].remove(alias)
        self.save_doctype(dt)
        return dt.learning

    def payload(self, fl: Flow, doc_id: str, status: str) -> dict:
        d = self.document(doc_id)
        dtn = None
        if d.get("doctype_id"):
            try:
                dtn = self.doctype(d["doctype_id"]).name
            except InvalidInput:
                pass
        return {"event": f"document.{status}", "flowId": fl.id, "flowName": fl.name, "flowVersion": d.get("instance_version"),
                "documentId": doc_id, "parentId": d.get("parent_id"), "fileName": d.get("file_name"),
                "docType": d.get("doctype_id"), "docTypeName": dtn, "status": status, "fields": d["fields"],
                "confidence": d["confidence"], "flags": d["flags"], "source": d.get("source"),
                "validatedBy": d.get("validated_by"), "processedAt": d.get("updated_at"),
                "fileUrl": f"/api/docs/v1/documents/{doc_id}/file"}

    def _deliver(self, fl: Flow, doc_id: str, status: str, only: Optional[str] = None) -> list[dict]:
        results = []
        for dv in fl.deliveries:
            if not dv.enabled or (only and dv.id != only) or (not only and status not in dv.on):
                continue
            ok, detail = False, ""
            try:
                body = self.payload(fl, doc_id, status)
                if dv.type in ("webhook", "start_bot"):
                    if dv.type == "start_bot":
                        body = {"bot": dv.params.get("bot", ""), "input": body}
                    headers = {"Content-Type": "application/json"}
                    if dv.params.get("authHeader"):
                        headers["Authorization"] = dv.params["authHeader"]
                    for attempt in range(2):
                        try:
                            r = httpx.post(dv.params["url"], json=body, headers=headers, timeout=15)
                            ok, detail = r.status_code < 300, f"HTTP {r.status_code}"
                            if ok:
                                break
                        except httpx.HTTPError as e:
                            detail = f"{type(e).__name__}: {e}"
                        time.sleep(1)
                elif dv.type == "folder":
                    folder = Path(dv.params["path"]) / status
                    folder.mkdir(parents=True, exist_ok=True)
                    stem = Path(body["fileName"] or doc_id).stem
                    (folder / f"{stem}_{doc_id[:8]}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2,
                                                                                 default=str), encoding="utf-8")
                    f = self.store.file_bytes(doc_id)
                    if f:
                        (folder / f"{stem}_{doc_id[:8]}{Path(f[1]).suffix}").write_bytes(f[0])
                    if status in ("validated", "invalid"):
                        for p in (Path(dv.params["path"]) / "validation").glob(f"{stem}_{doc_id[:8]}*"):
                            p.unlink(missing_ok=True)
                    ok, detail = True, str(folder)
                elif dv.type == "excel":
                    self._append_excel(Path(dv.params["path"]), body)
                    ok, detail = True, dv.params["path"]
            except Exception as e:
                detail = f"{type(e).__name__}: {e}"
            self.store.delivery_log(fl.id, doc_id, dv.name or dv.type, dv.type, ok, detail)
            results.append({"delivery": dv.name or dv.type, "type": dv.type, "ok": ok, "detail": detail})
        return results

    def _append_excel(self, path: Path, body: dict) -> None:
        from openpyxl import Workbook, load_workbook
        with self._xlsx_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            wb = load_workbook(path) if path.exists() else Workbook()
            sheet = re.sub(r"[\[\]:*?/\\]", "", body.get("docTypeName") or "Documents")[:31] or "Documents"
            fresh = sheet not in wb.sheetnames
            if fresh:
                ws = wb.create_sheet(sheet)
                if "Sheet" in wb.sheetnames and wb["Sheet"].max_row == 1 and wb["Sheet"].max_column == 1 \
                        and wb["Sheet"].cell(1, 1).value is None:
                    del wb["Sheet"]
            else:
                ws = wb[sheet]
            scal = {k: v for k, v in body["fields"].items() if not isinstance(v, (list, dict))}
            if fresh:
                head = ["document_id", "file_name", "status", "processed_at"] + list(scal)
                ws.append(head)
            else:
                head = [c.value for c in ws[1]]
            for k in scal:
                if k not in head:
                    head.append(k)
                    ws.cell(row=1, column=len(head), value=k)
            row = {"document_id": body["documentId"], "file_name": body["fileName"], "status": body["status"],
                   "processed_at": body["processedAt"], **scal}
            ws.append([row.get(h) for h in head])
            wb.save(path)

    def test_delivery(self, flow_id: str, delivery_id: str) -> list[dict]:
        fl = self.flow(flow_id)
        docs = self.store.instance_documents(flow_id, ["success", "validated"], limit=1)
        if not docs:
            raise InvalidInput("process at least one document in this flow first")
        return self._deliver(fl, docs[0]["id"], docs[0]["status"], only=delivery_id)

    # ---------------------------------------------------------------- teach from samples
    def suggest(self, files: list[tuple[bytes, str]]) -> dict:
        if not files:
            raise InvalidInput("upload 1 to 3 sample documents")
        results = []
        for data, name in files[:3]:
            results.append(self.svc.process_bytes(data, name, ProcessOptions(languages=["en", "ar"])))
        proposal = suggest_from_results(results)
        llm = next((p for p in self.svc.registry.available("extraction")
                    if p.info.optionPack == "tier3_ai" and self.svc.policy.allows(p.info)
                    and hasattr(p.instance, "_chat")), None)
        if llm:
            try:
                proposal = self._ai_suggest(llm, results, proposal)
            except Exception as e:
                proposal["aiError"] = f"{type(e).__name__}: {e}"
        proposal["sampleText"] = [r.text[:3000] for r in results]
        return proposal

    def _ai_suggest(self, llm, results, base: dict) -> dict:
        prompt = ("You design data-extraction schemas. From the OCR text of sample documents below, propose the fields "
                  "a business would extract. Reply with JSON: {\"name\": str, \"category\": one of "
                  f"{list(LIBRARY) + ['custom']}, \"keywords\": [str], \"fields\": [{{\"name\": snake_case, \"type\": "
                  "string|number|currency|date|enum|table, \"label\": \"English / Arabic label as printed\", "
                  "\"required\": bool, \"columns\": [{\"name\", \"type\"}] for tables}]}.\n\n" +
                  "\n\n---\n\n".join(r.text[:6000] for r in results))
        data = json.loads(re.search(r"\{.*\}", llm.instance._chat(prompt), re.S).group(0))
        fields = []
        for f in data.get("fields", []):
            if _snake(f.get("name", "")):
                fields.append({"name": _snake(f["name"]), "type": f.get("type", "string") if f.get("type") in
                               ("string", "number", "currency", "date", "enum", "table", "boolean") else "string",
                               "label": f.get("label"), "required": bool(f.get("required")), "aliases": [],
                               "columns": [{"name": _snake(c.get("name", "")) or "col", "type": c.get("type", "string")}
                                           for c in f.get("columns", [])] if f.get("type") == "table" else None,
                               "source": "AI suggestion"})
        return {**base, "name": data.get("name") or base["name"], "category": data.get("category", base["category"]),
                "keywords": data.get("keywords") or base["keywords"], "fields": fields or base["fields"],
                "method": f"ai:{llm.info.id}"}

    # ---------------------------------------------------------------- export & stats
    def export(self, flow_id: str, fmt: str = "csv", statuses: Optional[list[str]] = None) -> tuple[bytes, str, str]:
        fl = self.flow(flow_id)
        docs = self.store.instance_documents(flow_id, statuses or ["success", "validated"], limit=100000)
        rows, cols = [], []
        for d in docs:
            res = self.store.get_result(d["id"])
            vals = {f.name: f.value for f in res.fields} if res else {}
            for k in vals:
                if k not in cols:
                    cols.append(k)
            rows.append((d, vals))
        head = ["document_id", "file_name", "doc_type", "status", "processed_at", "validated_by"] + cols
        table = [[d["id"], d["file_name"], d["doctype_id"] or "", d["status"], d["updated_at"], d["validated_by"] or ""] +
                 [json.dumps(v.get(c), ensure_ascii=False) if isinstance(v.get(c), (list, dict)) else
                  ("" if v.get(c) is None else v.get(c)) for c in cols] for d, v in rows]
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        if fmt == "xlsx":
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "documents"
            ws.append(head)
            for r in table:
                ws.append(r)
            buf = io.BytesIO()
            wb.save(buf)
            return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"{fl.id}-{stamp}.xlsx"
        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(head)
        w.writerows(table)
        return ("﻿" + s.getvalue()).encode("utf-8"), "text/csv; charset=utf-8", f"{fl.id}-{stamp}.csv"

    def overview(self) -> dict:
        flows = self.flows()
        per = []
        for f in flows:
            st = self.store.instance_stats(f.id)
            per.append({"id": f.id, "name": f.name, "isInbox": f.isInbox, "status": f.status, "stats": st})
        q = self.review_queue(None)
        return {"totals": self.store.instance_stats(None), "flows": per, "review": len(q),
                "overdue": sum(1 for x in q if x["overdue"]), "doctypes": len(self.doctypes()),
                "recent": self.store.instance_documents(None, None, limit=12),
                "deliveryFailures": sum(1 for d in self.store.delivery_list(None, 200) if not d["ok"])}

    # ---------------------------------------------------------------- folders + SLA watcher
    def scan_folder(self, fl: Flow, require_live: bool = True) -> list[dict]:
        src = Path(fl.intake.inputFolder) if fl.intake.inputFolder else None
        if not src or not src.is_dir():
            return []
        done = src / "processed"
        done.mkdir(exist_ok=True)
        out = []
        for p in sorted(src.iterdir()):
            if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXT or time.time() - p.stat().st_mtime < 2:
                continue
            try:
                data = p.read_bytes()
                target = done / p.name
                if target.exists():
                    target = done / f"{p.stem}_{uuid.uuid4().hex[:6]}{p.suffix}"
                shutil.move(str(p), str(target))
                out.append({"file": p.name, **self.upload(fl.id, data, p.name, source="folder", require_live=require_live)})
            except Exception as e:
                log.exception("folder intake failed for %s", p)
                out.append({"file": p.name, "error": str(e)})
        return out

    def check_sla(self) -> int:
        n = 0
        for fl in self.flows():
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=fl.review.slaHours)).isoformat()
            for d in self.store.overdue_reviews(fl.id, cutoff):
                if fl.review.slaWebhook:
                    try:
                        httpx.post(fl.review.slaWebhook, json={"event": "review.overdue", "flowId": fl.id,
                                                               "flowName": fl.name, "documentId": d["id"],
                                                               "fileName": d["file_name"], "receivedAt": d["created_at"],
                                                               "slaHours": fl.review.slaHours}, timeout=10)
                    except httpx.HTTPError:
                        log.warning("SLA webhook failed for %s", d["id"])
                self.store.set_doc_fields(d["id"], sla_notified=1)
                n += 1
        return n

    def start_watcher(self, interval: float = 5.0) -> None:
        if self._watcher and self._watcher.is_alive():
            return

        def loop():
            last_sla = 0.0
            while not self._stop.wait(interval):
                for fl in self.flows():
                    if fl.status != "live" or not fl.intake.inputFolder:
                        continue
                    if time.time() - self._last_poll.get(fl.id, 0) < max(5, fl.intake.pollSeconds):
                        continue
                    self._last_poll[fl.id] = time.time()
                    try:
                        self.scan_folder(fl)
                    except Exception:
                        log.exception("watcher failed for %s", fl.id)
                if time.time() - last_sla > 60:
                    last_sla = time.time()
                    try:
                        self.check_sla()
                    except Exception:
                        log.exception("SLA check failed")

        self._watcher = threading.Thread(target=loop, name="docflow-watcher", daemon=True)
        self._watcher.start()

    def stop_watcher(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------------- migration from v2 learning instances
    def migrate_instances(self) -> int:
        n = 0
        for body in self.store.list_instance_bodies():
            try:
                inst = json.loads(body)
            except ValueError:
                continue
            if self.store.df_get("flow", inst["id"]):
                continue
            dt = self.save_doctype(DocType(id=f"{inst['id']}-type", name=inst["name"], description=inst.get("description", ""),
                                           category=inst.get("documentType", "custom"),
                                           keywords=LIBRARY.get(inst.get("documentType", ""), {}).get("keywords", []),
                                           languages=inst.get("languages", ["en"]),
                                           fields=[FieldDef.model_validate(f) for f in inst.get("fields", [])],
                                           checks=inst.get("validation", [])), new=True)
            eng = inst.get("engine", {})
            self.save_flow(Flow(id=inst["id"], name=inst["name"], description=inst.get("description", ""), docTypes=[dt.id],
                                engine=EngineChoice(**{k: v for k, v in eng.items() if k in EngineChoice.model_fields}),
                                review=ReviewSettings(fieldConfidence=inst.get("fieldConfidence", 0.85),
                                                      documentConfidence=inst.get("documentConfidence", 0.8)),
                                intake=Intake(inputFolder=inst.get("folders", {}).get("inputFolder", "")),
                                deliveries=[Delivery(type="folder", name="Output folder",
                                                     params={"path": inst["folders"]["outputFolder"]},
                                                     on=["success", "validation", "validated", "invalid", "failed"])]
                                if inst.get("folders", {}).get("outputFolder") else [],
                                status="live" if inst.get("status") == "published" else "draft"), new=True)
            n += 1
        return n
