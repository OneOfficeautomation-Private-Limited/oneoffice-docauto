"""Line-item tables from OCR geometry (CPU, no model): for engines that read words but not tables (Tesseract,
PaddleOCR), so scanned invoices still get their line items.

For each table field of the schema, on each page without tables:
1. the page's words are grouped into visual rows (same height on the page, whatever OCR line they are in);
2. the header row is the row where the captions of at least two columns are printed (the field's column labels and
   aliases, e.g. Description / Qty / Unit Price / Amount, الوصف / الكمية / المبلغ), and little else;
3. the captions' x-positions make the column bands (boundaries half-way between neighbouring captions);
4. the rows below it are read until a totals line (Subtotal / Total / VAT... / المجموع), a large gap or the page
   end; each word goes to the band holding its centre, and a row with only a description continues the one above;
5. the result is a Table (header row + one row per item) with a box and confidence per cell, which the rules
   extractor reads like any engine's table (numbers are coerced there), and grounding boxes each row and cell.
"""
from __future__ import annotations

import re
import statistics
from typing import Optional

from .extraction import _labels, _norm, label_matches
from .models import BBox, Cell, DocumentResult, FieldDef, FieldSchema, Page, Table, Word
from .normalize import detect_direction, union_bbox

# the first words of a line that ends the items: totals, tax, amount due
_TOTALS = re.compile(r"(?i)^(?:sub[\s-]*total|total|grand\s+total|net\s+total|vat|tax|amount\s+due|balance(?:\s+due)?|"
                     r"discount|shipping|المجموع|الإجمالي|الاجمالي|المجموع الفرعي|ضريبة|الضريبة|المبلغ المستحق|الخصم)\b")
MIN_COLUMNS = 2            # captions of at least this many columns make a header row
HEADER_SHARE = 0.6         # ...and they are most of the row's words (not "Total Amount: 1,000")
CONFIDENCE = 0.85          # a table rebuilt from layout: a little less sure than the words themselves


def _rows(page: Page) -> list[list[Word]]:
    """The page's words grouped into visual rows, top to bottom, each row left to right."""
    words = list(page.words)
    if not words:   # engines without words: the line's words with boxes split by their length
        for line in page.lines:
            toks = line.text.split()
            total = sum(len(t) for t in toks) + max(0, len(toks) - 1)
            x = line.bbox.x
            for t in toks:
                w = line.bbox.w * len(t) / total if total else 0
                words.append(Word(text=t, bbox=BBox(x=x, y=line.bbox.y, w=w, h=line.bbox.h), confidence=line.confidence))
                x += w + (line.bbox.w / total if total else 0)
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.bbox.y + w.bbox.h / 2, w.bbox.x)):
        cy = w.bbox.y + w.bbox.h / 2
        if rows:
            ref = rows[-1]
            rcy = statistics.mean(r.bbox.y + r.bbox.h / 2 for r in ref)
            if abs(cy - rcy) < max(w.bbox.h, max(r.bbox.h for r in ref)) * 0.5:
                ref.append(w)
                continue
        rows.append([w])
    for r in rows:
        r.sort(key=lambda w: w.bbox.x)
    return rows


def _caption(row: list[Word], labels: list[str], used: set[int]) -> Optional[tuple[int, int]]:
    """Where in the row one of the labels is printed: (first word, last word + 1), or None."""
    best = None
    for lbl in labels:
        n = len(_norm(lbl).split())
        if not n:
            continue
        for i in range(len(row) - n + 1):
            span = set(range(i, i + n))
            if span & used:
                continue
            if label_matches(" ".join(w.text for w in row[i:i + n]), lbl) and (best is None or n > best[1] - best[0]):
                best = (i, i + n)
    return best


def _header(row: list[Word], columns: list[FieldDef]) -> Optional[list[tuple[FieldDef, int, int]]]:
    """The columns whose captions this row prints, left to right: [(column, first word, end word)], or None."""
    found, used = [], set()
    # longest labels first, so "Unit Price" is not taken by a "Price" alias of another column
    for c in sorted(columns, key=lambda c: -max((len(_norm(l).split()) for l in _labels(c)), default=0)):
        hit = _caption(row, _labels(c), used)
        if hit:
            found.append((c, *hit))
            used |= set(range(*hit))
    if len(found) < min(MIN_COLUMNS, len(columns)) or len(used) < HEADER_SHARE * len(row):
        return None
    return sorted(found, key=lambda f: row[f[1]].bbox.x)


def _box(words: list[Word]) -> BBox:
    return union_bbox(w.bbox for w in words)


def read_table(page: Page, field: FieldDef) -> Optional[Table]:
    """The field's table on this page, rebuilt from the words' geometry, or None (no header row found)."""
    if not field.columns:
        return None
    rows = _rows(page)
    for hi, row in enumerate(rows):
        head = _header(row, field.columns)
        if head is None:
            continue
        spans = [(row[a].bbox.x, row[b - 1].bbox.x + row[b - 1].bbox.w) for _, a, b in head]
        bounds = [(spans[k][1] + spans[k + 1][0]) / 2 for k in range(len(spans) - 1)]
        left, right = min(s[0] for s in spans) - 0.05, max(s[1] for s in spans) + 0.1
        desc = next((k for k, (c, _, _) in enumerate(head) if c.type not in ("number", "currency")), 0)
        numeric = [k for k, (c, _, _) in enumerate(head) if c.type in ("number", "currency")]
        heights = [w.bbox.h for w in row]
        line_h = statistics.median(heights)
        items: list[list[list[Word]]] = []
        last_y = row[0].bbox.y
        for r in rows[hi + 1:]:
            y = min(w.bbox.y for w in r)
            if y - last_y > max(6 * line_h, 0.08):
                break                       # a large gap: the table is over
            text = _text(r)
            if _header(r, field.columns) is not None:
                break                       # the next table
            cells: list[list[Word]] = [[] for _ in head]
            for w in r:
                cx = w.bbox.x + w.bbox.w / 2
                if not left <= cx <= right:
                    continue
                cells[sum(cx > b for b in bounds)].append(w)
            first = _text(cells[desc]).strip()
            if _TOTALS.match(text) and (re.search(r"[:%：]", text) or _TOTALS.fullmatch(first.rstrip(":： ")) or
                                        sum(1 for c in cells if c) <= 2):
                break                       # Subtotal / Total / VAT...: the items are over
            if not any(cells):
                continue
            if items and not any(cells[k] for k in numeric) and sum(1 for c in cells if c) == 1:
                k = next(k for k, c in enumerate(cells) if c)
                items[-1][k].extend(cells[k])  # a wrapped description: part of the item above
            else:
                items.append(cells)
            last_y = y
        if not items:
            continue
        out = [Cell(row=0, col=k, text=_text(row[a:b]), isHeader=True, bbox=_box(row[a:b]),
                    confidence=_conf(row[a:b])) for k, (_, a, b) in enumerate(head)]
        for i, cells in enumerate(items, 1):
            out += [Cell(row=i, col=k, text=_text(ws), bbox=_box(ws) if ws else None,
                         confidence=_conf(ws)) for k, ws in enumerate(cells)]
        confs = [c.confidence for c in out if c.confidence is not None and not c.isHeader]
        return Table(rows=len(items) + 1, cols=len(head), cells=out,
                     bbox=union_bbox(c.bbox for c in out if c.bbox),
                     confidence=round(CONFIDENCE * statistics.mean(confs), 4) if confs else None)
    return None


def _text(words: list[Word]) -> str:
    """Words (left to right on the page) in reading order: right to left for Arabic."""
    text = " ".join(w.text for w in words)
    return " ".join(w.text for w in reversed(words)) if detect_direction(text) == "rtl" else text


def _conf(words: list[Word]) -> Optional[float]:
    vals = [w.confidence for w in words if w.confidence is not None]
    return round(statistics.mean(vals), 4) if vals else None


def add_line_tables(doc: DocumentResult, schema: Optional[FieldSchema]) -> int:
    """Rebuild the schema's tables on pages that have none (OCR engines without table reading). Returns how many
    tables were added."""
    fields = [f for f in (schema.fields if schema else []) if f.type == "table" and f.columns]
    added = 0
    for page in doc.pages:
        if page.tables:
            continue
        for f in fields:
            t = read_table(page, f)
            if t is not None and not any(t.bbox == x.bbox for x in page.tables):
                page.tables.append(t)
                added += 1
    return added
