"""Field grounding: find each extracted value on the page, so a review screen can highlight it.

After extraction (rules or any AI engine) every value is looked up in the page words. Arabic-Indic
digits and separators, thousands/decimal styles, spacing, case and the common date formats are
normalised, and small OCR slips are tolerated for text. A value that is found gets the page number
and the bounding box of the matched words (normalized 0-1, union of the words) and grounded=True.
Table rows are found line by line: each row gets the box of its cells, and each cell its own box.

AI values that are not found keep the hallucination guard (confidence halved and a validation
error, so the document goes to review); AI values the shared guard missed because of formatting
(a date printed 15/10/2026, read as 2026-10-15) get their confidence back.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Optional

from .extraction import _AR_DIGITS, _to_float, coerce
from .models import BBox, DocumentResult, ExtractedField, FieldSchema, Location, Page, RowLocation
from .normalize import union_bbox

HALLUCINATION = "value not found in OCR text (possible hallucination)"
FUZZY = 0.88          # text similarity accepted as the same value (OCR slips), for values of 5+ characters
MAX_CONFIDENCE = 0.99
_SEPARATORS = str.maketrans({"٫": ".", "٬": ",", "،": ",", " ": " ", " ": " "})
_CUR = r"(?:SAR|AED|USD|EUR|GBP|INR|QAR|KWD|BHD|OMR|EGP|JOD|Rs\.?|ر\.?س\.?|ريال|د\.?إ\.?|[$€£₹﷼])"
_CURRENCY = re.compile(rf"(?i)^{_CUR}\s*|\s*{_CUR}$")
_NUMBER = re.compile(r"[-+]?\d[\d,.\s]*\d|\d")
_INNER_NUMBER = re.compile(r"\d[\d,.]*\d|\d")
_EDGE = " :;,()[]{}*#|\"'"


def norm(s: Any) -> str:
    """Text as compared: Western digits, '.' and ',' separators, NFKC, single spaces."""
    t = unicodedata.normalize("NFKC", str(s)).translate(_AR_DIGITS).translate(_SEPARATORS)
    return re.sub(r"\s+", " ", t).strip()


def key(s: Any) -> str:
    """Letters and digits only, case-folded: 'INV - 2026/42' and 'inv2026 42' compare equal."""
    return "".join(ch for ch in norm(s).casefold() if ch.isalnum())


def as_number(text: str) -> Optional[float]:
    """A whole window read as an amount ('SAR 11,500.00', '١١٬٥٠٠٫٠٠', '1 500.00'), or None."""
    t = norm(text).strip(_EDGE)
    for _ in range(2):  # currency before and/or after the amount
        t = _CURRENCY.sub("", t).strip(_EDGE)
    if not _NUMBER.fullmatch(t):
        return None
    try:
        return _to_float(t)
    except ValueError:
        return None


def _value_kind(value: Any, ftype: str) -> Optional[str]:
    if value in (None, "", []) or isinstance(value, bool) or ftype in ("boolean", "table", "object"):
        return None
    if isinstance(value, (int, float)):
        return "number"
    if ftype in ("number", "currency"):
        return "number" if as_number(str(value)) is not None else "text"
    if ftype == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        return "date"
    return "text"


class _Line:
    """One OCR line as tokens (word text + box) with precomputed lookups for fast matching."""

    def __init__(self, page: Page, idx: int):
        line = page.lines[idx]
        self.page, self.idx = page.pageNumber, idx
        if line.wordIndexes and page.words:
            self.tokens = [(norm(page.words[i].text), page.words[i].bbox) for i in line.wordIndexes
                           if i < len(page.words)]
        else:  # engines without words: tokens of the line text, each with the line's box
            self.tokens = [(t, line.bbox) for t in norm(line.text).split()]
        self.text_key = key(line.text)
        self.keys: set[str] = set()
        self.numbers: set[float] = set()
        self._dates: Optional[set[str]] = None
        n = len(self.tokens)
        for i in range(n):
            for j in range(i + 1, min(n, i + 3) + 1):
                text = " ".join(t for t, _ in self.tokens[i:j])
                self.keys.add(key(text))
                if j - i <= 2 and (v := as_number(text)) is not None:
                    self.numbers.add(round(v, 2))
            for m in _INNER_NUMBER.finditer(self.tokens[i][0]):
                try:
                    self.numbers.add(round(_to_float(m.group(0)), 2))
                except ValueError:
                    pass

    def dates(self) -> set[str]:
        if self._dates is None:
            self._dates = set()
            n = len(self.tokens)
            for i in range(n):
                for j in range(i + 1, min(n, i + 3) + 1):
                    text = " ".join(t for t, _ in self.tokens[i:j]).strip(_EDGE)
                    if re.search(r"\d", text):
                        v, ok = coerce(text, "date")
                        if ok:
                            self._dates.add(v)
        return self._dates

    def contains(self, value: Any, kind: str) -> bool:
        if kind == "number":
            v = _num(value)
            return v is not None and v in self.numbers
        if kind == "date":
            return value in self.dates()
        k = key(value)
        return bool(k) and (k in self.keys or (len(k) >= 4 and k in self.text_key))

    def locate(self, value: Any, kind: str, fuzzy: bool = True) -> Optional[tuple[float, int, BBox]]:
        """Best window of tokens holding the value: (score, words used, box) or None."""
        best = None
        n = len(self.tokens)
        if kind == "number":
            want = _num(value)
            if want is None:
                return None
        elif kind == "text":
            want = key(value)
            if not want:
                return None
            nv = max(1, len(norm(value).split()))
        for i in range(n):
            if kind == "number":
                spans = range(1, 4)
            elif kind == "date":
                spans = range(1, 5)
            else:
                spans = range(max(1, nv - 1), nv + 2)
            for size in spans:
                j = i + size
                if j > n:
                    break
                text = " ".join(t for t, _ in self.tokens[i:j])
                score = 0.0
                if kind == "number":
                    v = as_number(text)
                    if v is not None and round(v, 2) == want:
                        score = 1.0
                    elif size == 1 and any(_inner(m) == want for m in _INNER_NUMBER.finditer(text)):
                        score = 0.9   # the amount is glued to a label: 'Total:11,500.00'
                elif kind == "date":
                    t = text.strip(_EDGE)
                    if re.search(r"\d", t) and coerce(t, "date") == (value, True):
                        score = 1.0
                else:
                    k = key(text)
                    if k == want:
                        score = 1.0
                    elif size == 1 and len(want) >= 4 and want in k:
                        score = 0.9   # value inside a longer token: 'No:INV-7'
                    elif fuzzy and len(want) >= 5:
                        sm = SequenceMatcher(None, k, want)
                        if sm.real_quick_ratio() >= FUZZY and sm.quick_ratio() >= FUZZY and sm.ratio() >= FUZZY:
                            score = round(sm.ratio() * 0.95, 4)
                if score and (best is None or (score, -size) > (best[0], -best[1])):
                    best = (score, size, union_bbox(b for _, b in self.tokens[i:j]))
        return best


def _num(value: Any) -> Optional[float]:
    v = value if isinstance(value, (int, float)) and not isinstance(value, bool) else as_number(str(value))
    return None if v is None else round(float(v), 2)


def _inner(m: re.Match) -> Optional[float]:
    try:
        return round(_to_float(m.group(0)), 2)
    except ValueError:
        return None


def _overlap(a: Optional[BBox], b: Optional[BBox], margin: float = 0.01) -> bool:
    if a is None or b is None:
        return False
    return not (a.x + a.w + margin < b.x or b.x + b.w + margin < a.x or
                a.y + a.h + margin < b.y or b.y + b.h + margin < a.y)


class Grounder:
    """Finds values on the pages of one document (lines indexed once, reused for every field)."""

    def __init__(self, doc: DocumentResult):
        self.lines = [_Line(p, i) for p in doc.pages for i in range(len(p.lines))]

    def find(self, value: Any, kind: str, hint_page: Optional[int] = None,
             hint_bbox: Optional[BBox] = None) -> Optional[tuple[int, BBox, float]]:
        """(page, box, score) of the best place the value is printed, or None. A place inside the box the
        extractor reported (its hint) wins over the same value printed elsewhere."""
        cands = [l for l in self.lines if l.contains(value, kind)]
        fuzzy_pass = False
        if not cands and kind == "text" and len(key(value)) >= 5:
            cands, fuzzy_pass = self.lines, True
        best, best_key = None, None
        for l in cands:
            hit = l.locate(value, kind, fuzzy=fuzzy_pass)
            if hit is None:
                continue
            score, size, box = hit
            near = 2 if l.page == hint_page and _overlap(box, hint_bbox) else 1 if l.page == hint_page else 0
            k = (near, score, -size)
            if best_key is None or k > best_key:
                best, best_key = (l.page, box, score), k
        return best

    def find_row(self, row: dict, kinds: dict[str, str], after: int) -> Optional[tuple[int, list[tuple[str, int, BBox]]]]:
        """The line holding most of a table row's cells (rows are printed in order: lines after the previous
        row win ties). Returns (line position, [(column, page, box)])."""
        cells = {c: v for c, v in row.items() if kinds.get(c)}
        if not cells:
            return None
        best, best_key = None, None
        for pos, l in enumerate(self.lines):
            hits = sum(1 for c, v in cells.items() if l.contains(v, kinds[c]))
            if not hits:
                continue
            k = (hits, pos > after, -abs(pos - after))
            if best_key is None or k > best_key:
                best, best_key = pos, k
        if best is None or best_key[0] < max(1, (len(cells) + 1) // 2):
            return None
        found = []
        main = self.lines[best]
        for c, v in cells.items():
            # a wrapped description can sit on the line below or above the amounts
            for l in (main, *[self.lines[p] for p in (best + 1, best - 1) if 0 <= p < len(self.lines)
                              and self.lines[p].page == main.page]):
                hit = l.locate(v, kinds[c], fuzzy=l is main)
                if hit:
                    found.append((c, l.page, hit[2]))
                    break
        return best, found


def _column_kinds(fld: ExtractedField, schema: Optional[FieldSchema]) -> dict[str, str]:
    fd = next((f for f in schema.fields if f.name == fld.name), None) if schema else None
    types = {c.name: c.type for c in fd.columns} if fd else {}
    kinds: dict[str, str] = {}
    for row in fld.value if isinstance(fld.value, list) else []:
        if not isinstance(row, dict):
            continue
        for c, v in row.items():
            if c not in kinds and (k := _value_kind(v, types.get(c, "string"))):
                kinds[c] = k
    return kinds


def ground_table(g: Grounder, fld: ExtractedField, schema: Optional[FieldSchema]) -> None:
    rows = fld.value if isinstance(fld.value, list) else []
    if not rows or not all(isinstance(r, dict) for r in rows):
        return
    kinds = _column_kinds(fld, schema)
    out, after = [], -1
    for i, row in enumerate(rows):
        hit = g.find_row({c: row[c] for c in kinds if row.get(c) not in (None, "")}, kinds, after)
        if hit is None:
            out.append(RowLocation(row=i))
            continue
        after, found = hit
        cells = {c: Location(page=p, bbox=b, grounded=True) for c, p, b in found}
        page = found[0][1] if found else None
        wanted = sum(1 for c in kinds if row.get(c) not in (None, ""))
        out.append(RowLocation(row=i, page=page, bbox=union_bbox(b for _, p, b in found if p == page),
                               grounded=len(found) * 2 >= max(1, wanted), cells=cells))
    fld.rowLocations = out
    fld.grounded = all(r.grounded for r in out)
    fld.groundedBy = "ocr" if fld.grounded else None
    placed = [r for r in out if r.page is not None]
    if placed and fld.page is None:
        fld.page = placed[0].page
        fld.bbox = union_bbox(r.bbox for r in placed if r.page == fld.page and r.bbox)


def ground_field(g: Grounder, fld: ExtractedField) -> Optional[bool]:
    """Ground one scalar field in place. Returns grounded (None = nothing to check)."""
    kind = _value_kind(fld.value, fld.type)
    if kind is None:
        fld.grounded = fld.groundedBy = None
        return None
    hit = g.find(fld.value, kind, fld.page, fld.bbox)
    if hit is None and kind == "date" and fld.rawText and fld.rawText != fld.value:
        hit = g.find(fld.rawText, "text", fld.page, fld.bbox)   # the printed form the extractor saw
    if hit is None:
        fld.grounded, fld.groundedBy = False, None
        return False
    fld.page, fld.bbox = hit[0], hit[1]
    fld.grounded, fld.groundedBy = True, "ocr"
    return True


def ground_document(doc: DocumentResult, schema: Optional[FieldSchema] = None, ai: bool = False,
                    only: Optional[set[str]] = None) -> None:
    """Ground every field of the document in place. ai=True applies the hallucination guard: values that are
    not on the page lose half their confidence and must be reviewed; values the AI engine's own check missed
    (formatting) are cleared again. only: ground just these fields (e.g. reviewer corrections)."""
    if not doc.pages:
        return
    g = Grounder(doc)
    for fld in doc.fields:
        if only is not None and fld.name not in only:
            continue
        if fld.type == "table" or isinstance(fld.value, list):
            ground_table(g, fld, schema)
            continue
        found = ground_field(g, fld)
        if not ai or found is None:
            continue
        if found and HALLUCINATION in fld.validation.errors:
            fld.validation.errors.remove(HALLUCINATION)
            fld.validation.passed = not fld.validation.errors
            if fld.confidence is not None:
                fld.confidence = round(min(MAX_CONFIDENCE, fld.confidence * 2), 4)
        elif not found and HALLUCINATION not in fld.validation.errors:
            fld.validation.errors.append(HALLUCINATION)
            fld.validation.passed = False
            if fld.confidence is not None:
                fld.confidence = round(fld.confidence * 0.5, 4)


def locations(doc: DocumentResult) -> dict[str, dict]:
    """Per field: where it is on the pages, for the API ({name: {page, bbox, grounded, groundedBy, rows?}})."""
    out = {}
    for f in doc.fields:
        loc = {"page": f.page, "bbox": f.bbox.model_dump() if f.bbox else None, "grounded": f.grounded,
               "groundedBy": f.groundedBy}
        if f.rowLocations is not None:
            loc["rows"] = [r.model_dump() for r in f.rowLocations]
        out[f.name] = loc
    return out
