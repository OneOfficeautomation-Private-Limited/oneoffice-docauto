"""Built-in, offline extraction + classification (no AI required).

Strategy per field: key-value pairs -> regex -> 'Label: value' lines -> (optional) a fallback
that needs no label, e.g. the company name at the top of an invoice (FieldDef.fallback).
A value's OCR confidence is the confidence of the words that print the value, not of
the whole line: a poorly read label ('Subtotal:' at 0%) does not make a clearly read
amount doubtful. Tier 3 (LLM) extractors plug in through the same ExtractionProvider contract.
"""
from __future__ import annotations

import ast
import operator
import re
from datetime import datetime
from typing import Any, Optional

from .models import Classification, DocumentResult, ExtractedField, FieldDef, FieldSchema, Line, Page
from .provider import Capability, ExtractionProvider, ExtractRequest, ProviderInfo

_NUM = re.compile(r"[-+]?\d[\d,\s]*(?:[.,]\d+)*")
_DIGIT_GAP = re.compile(r"(?<=\d) (?=\d)")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d",
                 "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"]


def _to_float(s: str) -> float:
    """Number with thousands/decimal separators in any common style.

    1,234.50 / 1.234,50 / 6.257.13 (OCR read a comma as a dot) -> the last separator is the decimal
    mark, unless it is followed by exactly three digits and every separator is the same
    (1,234,567). A single '.' is decimal; a single ',' before three digits is thousands.
    """
    s = s.replace(" ", "").rstrip(",.")
    seps = [i for i, ch in enumerate(s) if ch in ".,"]
    if not seps:
        return float(s)
    last, tail = seps[-1], len(s) - seps[-1] - 1
    if len(seps) == 1:
        return float(s.replace(",", "") if s[last] == "," and tail == 3 else s.replace(",", "."))
    if tail == 3 and len({s[i] for i in seps}) == 1:
        return float(re.sub(r"[.,]", "", s))
    return float(re.sub(r"[.,]", "", s[:last]) + "." + s[last + 1:])


def coerce(value: str, ftype: str, fmt: Optional[str] = None) -> tuple[Any, bool]:
    """Return (typed value, ok)."""
    v = (value or "").strip().translate(_AR_DIGITS)
    if ftype in ("number", "currency"):
        m = _NUM.search(v)
        if not m:
            return value, False
        try:
            return _to_float(m.group(0)), True
        except ValueError:
            return value, False
    if ftype == "date":
        token = re.search(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}\s+\w+\s+\d{4}|\w+\s+\d{1,2},\s*\d{4}", v)
        cand = token.group(0) if token else v
        for f in ([fmt] if fmt else []) + _DATE_FORMATS:
            try:
                py_fmt = f.replace("YYYY", "%Y").replace("MM", "%m").replace("DD", "%d") if "%" not in f else f
                return datetime.strptime(cand, py_fmt).date().isoformat(), True
            except (ValueError, TypeError):
                continue
        return value, False
    if ftype == "boolean":
        low = v.lower()
        if low in ("yes", "true", "y", "1", "x", "✓", "نعم"):
            return True, True
        if low in ("no", "false", "n", "0", "لا"):
            return False, True
        return value, False
    return v, bool(v)


def _labels(f: FieldDef) -> list[str]:
    labels = [f.name.replace("_", " ")] + ([f.label] if f.label else []) + f.aliases
    out = []
    for l in labels:
        out.extend(p.strip() for p in l.split("/") if p.strip())
    return [l for l in out if l]


def _norm(s: str) -> str:
    return re.sub(r"[\s:_\-#.]+", " ", s.lower()).strip()


def label_matches(text: str, label: str) -> bool:
    """A printed caption (a table header cell) is this label: the same words, or a small OCR slip ('Descriptlon')."""
    a, b = _norm(text), _norm(label)
    if not a or not b:
        return False
    if a == b:
        return True
    from difflib import SequenceMatcher
    return len(b) >= 5 and SequenceMatcher(None, a, b).ratio() >= 0.85


def span_confidence(page: Page, line: Line, start: int, end: int) -> Optional[float]:
    """Mean OCR confidence of the words printing line.text[start:end] (the value, not its label). The line's own
    confidence when its words are not known or do not spell the line text."""
    words = [page.words[i] for i in line.wordIndexes if i < len(page.words)]
    if not words or " ".join(w.text for w in words) != line.text:
        return line.confidence
    pos, confs = 0, []
    for w in words:
        s, e = pos, pos + len(w.text)
        pos = e + 1
        if e > start and s < end and w.confidence is not None:
            confs.append(w.confidence)
    return round(sum(confs) / len(confs), 4) if confs else line.confidence


# the company name printed at the top of a page (FieldDef.fallback = "header_company")
_COMPANY = re.compile(r"(?i)(?<![\w])(?:llc|l\.l\.c\.?|ltd\.?|limited|co\.?|company|corp\.?|corporation|inc\.?|est\.?|"
                      r"establishment|trading|group|holding|industries|enterprises|gmbh|plc|llp|w\.?l\.?l\.?|fze|fzco|"
                      r"pvt\.?)(?![\w])|شركة|مؤسسة|مجموعة|المحدودة|للتجارة")
_TITLE = re.compile(r"(?i)\b(?:tax\s+)?invoice\b|\breceipt\b|\bpurchase\s+order\b|\bcredit\s+note\b|\bdebit\s+note\b|"
                    r"\bquotation\b|\bstatement\b|\bdelivery\s+note\b|فاتورة|إيصال|ايصال|أمر\s+شراء|إشعار")
_NOT_NAME = re.compile(r"(?i)@|www\.|https?:|\.com\b|\b(?:tel|phone|fax|mobile|e-?mail|p\.?\s?o\.?\s?box|street|st\.|road|"
                       r"rd\.|building|floor|district|city|vat|trn|page)\b|هاتف|جوال|ص\.?\s?ب|شارع|الرقم الضريبي|سجل")
HEADER_TOP = 0.3          # the company line sits in the top 30% of page 1
HEADER_CONFIDENCE = 0.75  # at most: a guess from layout, so a person checks it unless e-invoice data confirms it


def _find_label(text: str, label: str) -> Optional[tuple[int, int, bool]]:
    """Locate a label in a line as a whole word/phrase. Returns (start, end, fuzzy) or None."""
    pat = re.compile(r"(?<![\w])" + re.escape(label) + r"(?![\w])", re.IGNORECASE)
    m = pat.search(text)
    if m:
        return m.start(), m.end(), False
    if len(label) < 6:
        return None
    # fuzzy: compare token windows (handles OCR slips like 'Subtota]')
    from difflib import SequenceMatcher
    n = len(label.split())
    tokens = [(t.start(), t.end()) for t in re.finditer(r"\S+", text)]
    for k in range(len(tokens) - n + 1):
        s, e = tokens[k][0], tokens[k + n - 1][1]
        window = text[s:e].rstrip(":：")
        if re.search(r"[:：]", window):  # a label never spans the label/value separator
            continue
        if SequenceMatcher(None, window.lower(), label.lower()).ratio() >= 0.85:
            return s, e, True
    return None


class RulesExtractor(ExtractionProvider):
    """Deterministic extractor shipped with the core (works air-gapped)."""

    def extract(self, req: ExtractRequest) -> list[ExtractedField]:
        doc = req.document
        out = []
        for f in req.schema.fields:
            if f.type == "table":
                out.append(self._table(doc, f))
            else:
                out.append(self._scalar(doc, f))
        return out

    def _scalar(self, doc: DocumentResult, f: FieldDef) -> ExtractedField:
        labels = _labels(f)
        nl = [_norm(l) for l in labels]
        # 1) key-value pairs from structure-capable engines
        for p in doc.pages:
            for kv in p.keyValuePairs:
                if _norm(kv.key) in nl or any(n and n in _norm(kv.key) for n in nl):
                    val, ok = coerce(kv.value, f.type, f.format)
                    if ok:
                        return self._field(f, val, kv.value, kv.confidence, p.pageNumber, kv.valueBbox, 0.95)
        # 2) explicit regex, if given, has priority over label heuristics
        if f.regex:
            m = re.search(f.regex, doc.text, re.IGNORECASE | re.MULTILINE)
            if m:
                raw = m.group(1) if m.groups() else m.group(0)
                val, ok = coerce(raw, f.type, f.format)
                if ok:
                    page, line = self._locate(doc, raw)
                    conf = None
                    if line:
                        at = line.text.find(raw)
                        conf = span_confidence(page, line, at, at + len(raw))
                    return self._field(f, val, raw, conf, page.pageNumber if page else None,
                                       line.bbox if line else None, 0.9)
            # OCR often splits long numbers with a space ("35075291 7034233"): retry per line without them
            for p in doc.pages:
                for line in p.lines:
                    joined = _DIGIT_GAP.sub("", line.text)
                    m = re.search(f.regex, joined, re.IGNORECASE) if joined != line.text else None
                    if m:
                        raw = m.group(1) if m.groups() else m.group(0)
                        val, ok = coerce(raw, f.type, f.format)
                        if ok:
                            return self._field(f, val, raw, line.confidence, p.pageNumber, line.bbox, 0.9)
        # 3) "Label: value" on the same line, or value on the next line.
        #    Most specific label wins (longest, at line start); tolerant to small OCR errors.
        matches = []
        for p in doc.pages:
            for i, line in enumerate(p.lines):
                for label in labels:
                    hit = _find_label(line.text, label)
                    if hit is None:
                        continue
                    start, end, fuzzy = hit
                    s = len(label) + (5 if line.text[:start].strip(" :#-") == "" else 0) - (3 if fuzzy else 0)
                    matches.append((s, p, i, line, end, fuzzy))
        matches.sort(key=lambda m: (m[5], -m[0]))  # exact label matches before fuzzy ones
        for _, p, i, line, end, fuzzy in matches:
            rest = line.text[end:]
            colon = re.search(r"[:：]", rest)
            if colon and re.search(r"[A-Za-z\u0600-\u06FF]{2,}", rest[:colon.start()]):
                continue  # the printed label is longer ("Supplier VAT No:" is not "Supplier:")
            after = rest.lstrip(" :：#-\t")
            candidates = []   # (text, OCR confidence of the value's words, where it is)
            if after:
                at = len(line.text) - len(after)
                candidates.append((after, span_confidence(p, line, at, len(line.text)), line.bbox))
            if i + 1 < len(p.lines):
                nxt = p.lines[i + 1]
                candidates.append((nxt.text, nxt.confidence, nxt.bbox))
            for c, conf, box in candidates:
                val, ok = coerce(c, f.type, f.format)
                if ok and (f.type != "enum" or not f.enumValues or val in f.enumValues):
                    return self._field(f, val, c, conf, p.pageNumber, box, 0.75 if fuzzy else 0.9)
        # 4) enum without a label: a single allowed value printed anywhere (e.g. "Total: 11,500.00 SAR")
        if f.type == "enum" and f.enumValues:
            hits = {}
            for p in doc.pages:
                for line in p.lines:
                    for v in f.enumValues:
                        if v not in hits and re.search(r"(?<![\w])" + re.escape(v) + r"(?![\w])", line.text):
                            hits[v] = (p, line)
            if len(hits) == 1:
                v, (p, line) = next(iter(hits.items()))
                return self._field(f, v, line.text, line.confidence, p.pageNumber, line.bbox, 0.88)
        # 5) no label printed: a fallback the document type asks for
        if f.fallback == "header_company" and f.type == "string":
            hit = header_company(doc)
            if hit:
                p, line = hit
                conf = HEADER_CONFIDENCE * (line.confidence if line.confidence is not None else 0.9)
                return ExtractedField(name=f.name, type=f.type, value=line.text.strip(), rawText=line.text,
                                      confidence=round(conf, 4), page=p.pageNumber, bbox=line.bbox,
                                      providerId=self.info.id)
        return ExtractedField(name=f.name, type=f.type, value=None, confidence=0.0, providerId=self.info.id)

    def _table(self, doc: DocumentResult, f: FieldDef) -> ExtractedField:
        col_names = [c.name for c in f.columns]
        for p in doc.pages:
            for t in p.tables:
                grid = t.to_matrix()
                if not grid:
                    continue
                mapping = {}
                for c in f.columns:
                    at = next((k for lbl in _labels(c) for k, h in enumerate(grid[0])
                               if k not in mapping.values() and label_matches(h, lbl)), None)
                    if at is not None:
                        mapping[c.name] = at
                if not mapping:
                    continue
                rows = []
                for r in grid[1:]:
                    row = {}
                    for c in f.columns:
                        if c.name in mapping:
                            row[c.name], _ = coerce(r[mapping[c.name]], c.type, c.format)
                    rows.append(row)
                return ExtractedField(name=f.name, type="table", value=rows, confidence=t.confidence or 0.8,
                                      page=p.pageNumber, bbox=t.bbox, providerId=self.info.id,
                                      rawText=f"{len(rows)} rows; columns {list(mapping)} of {col_names}")
        return ExtractedField(name=f.name, type="table", value=[], confidence=0.0, providerId=self.info.id)

    def _locate(self, doc: DocumentResult, raw: str) -> tuple[Optional[Page], Optional[Line]]:
        for p in doc.pages:
            for l in p.lines:
                if raw in l.text:
                    return p, l
        return None, None

    def _field(self, f, val, raw, ocr_conf, page, bbox, strategy_conf) -> ExtractedField:
        conf = strategy_conf * (ocr_conf if ocr_conf is not None else 0.9)
        return ExtractedField(name=f.name, type=f.type, value=val, rawText=raw, confidence=round(conf, 4),
                              page=page, bbox=bbox, providerId=self.info.id)

    def classify(self, doc: DocumentResult, labels: list[str]) -> Classification:
        text = doc.text.lower()
        scores = []
        for label in labels:
            words = [w for w in re.split(r"[\s_/|,]+", label.lower()) if len(w) > 2]
            hits = sum(text.count(w) for w in words)
            scores.append((label, hits))
        total = sum(s for _, s in scores) or 1
        scores.sort(key=lambda x: -x[1])
        cands = [{"label": l, "confidence": round(s / total, 4)} for l, s in scores]
        best = cands[0] if scores and scores[0][1] > 0 else {"label": "other", "confidence": 0.0}
        return Classification(label=best["label"], confidence=best["confidence"], candidates=cands)


def header_company(doc: DocumentResult) -> Optional[tuple[Page, Line]]:
    """The company name printed as the heading of page 1 (an invoice's issuer), or None.

    Looks only above the title ('Tax Invoice', 'فاتورة', 'Receipt'...) or, without a title, at the first lines in the
    top of the page, and stops at the first labelled line ('X: ...'). A candidate is mostly letters: no date, amount
    or ID, no address or contact line. The most prominent wins: a company suffix (LLC, Ltd, Co., Est., Trading,
    شركة, مؤسسة...), then the tallest text, then the topmost. Without a suffix only the first heading line is taken."""
    page = next((p for p in doc.pages if p.lines), None)
    if page is None:
        return None
    cands = []
    for l in sorted((l for l in page.lines if l.bbox.y < HEADER_TOP), key=lambda l: (l.bbox.y, l.bbox.x)):
        t = l.text.strip()
        if re.search(r"[:：]", t):
            break               # labelled data starts: the heading is over
        if _TITLE.search(t):
            if cands:
                break           # the issuer's name is printed above the document title
            continue
        letters, digits = sum(ch.isalpha() for ch in t), sum(ch.isdigit() for ch in t)
        if (letters < 3 or digits > 2 or letters < 0.6 * len(t.replace(" ", "")) or len(t.split()) > 8
                or _NOT_NAME.search(t)):
            continue
        cands.append(l)
    if not cands:
        return None
    best = max(cands, key=lambda l: (bool(_COMPANY.search(l.text)), round(l.bbox.h, 3), -l.bbox.y))
    if not _COMPANY.search(best.text) and best is not cands[0]:
        return None
    return page, best


RULES_INFO = ProviderInfo(
    id="builtin-rules", displayName="Built-in rules extractor", version="1.0.0", vendor="Oneofficeautomation",
    tier=1, kind="extraction", capabilities=[Capability.EXTRACT_SCHEMA, Capability.CLASSIFY],
    languages=["*"], runsLocally=True, creditCostPerPage={"extract_schema": 0, "classify": 0},
    optionPack="core",
)


# ------------------------------------------------------------- validation rules

_ALLOWED_FUNCS = {"sum": sum, "round": round, "abs": abs, "len": len, "min": min, "max": max}
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos,
        ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge}


def safe_eval(expr: str, names: dict[str, Any]) -> Any:
    """Evaluate arithmetic/comparison rules over field values without exec()."""

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            if n.id not in names:
                raise NameError(n.id)
            return names[n.id]
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.BoolOp):
            vals = [ev(v) for v in n.values]
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, comp in zip(n.ops, n.comparators):
                right = ev(comp)
                if type(op) not in _OPS or not _OPS[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _ALLOWED_FUNCS:
            return _ALLOWED_FUNCS[n.func.id](*[ev(a) for a in n.args])
        if isinstance(n, ast.Attribute):  # table column access: line_items.amount
            base = ev(n.value)
            if isinstance(base, list):
                return [r.get(n.attr) for r in base if isinstance(r, dict) and r.get(n.attr) is not None]
        raise ValueError(f"unsupported expression element: {type(n).__name__}")

    return ev(ast.parse(expr, mode="eval"))
