"""Engines that ship inside the core (always available, 0 credits)."""
from __future__ import annotations

import io

from .models import DocumentResult, Line, Page, Word
from .normalize import bbox_from_pixels, detect_direction, detect_language, union_bbox
from .provider import Capability, OcrProvider, ProviderInfo, RecognizeRequest


class PdfTextProvider(OcrProvider):
    """Reads the embedded text layer of digital PDFs: no OCR, exact text, zero cost."""

    def recognize(self, req: RecognizeRequest) -> DocumentResult:
        import pdfplumber

        wanted = set(req.options.get("pages") or [])
        doc = DocumentResult(documentId=req.document_id)
        with pdfplumber.open(io.BytesIO(req.source_bytes or b"")) as pdf:
            for pno, page in enumerate(pdf.pages, 1):
                if wanted and pno not in wanted:
                    continue
                W, H = float(page.width), float(page.height)
                words = page.extract_words(keep_blank_chars=False, use_text_flow=True)
                pg = Page(pageNumber=pno, width=W, height=H, unit="point")
                rows: dict[int, list[dict]] = {}
                for w in words:
                    rows.setdefault(round(w["top"] / 3), []).append(w)
                for key in sorted(rows):
                    ws = sorted(rows[key], key=lambda w: w["x0"])
                    idx = []
                    for w in ws:
                        idx.append(len(pg.words))
                        pg.words.append(Word(text=w["text"], confidence=1.0,
                                             bbox=bbox_from_pixels(w["x0"], w["top"], w["x1"] - w["x0"],
                                                                   w["bottom"] - w["top"], W, H)))
                    text = " ".join(w["text"] for w in ws)
                    pg.lines.append(Line(text=text, confidence=1.0, wordIndexes=idx,
                                         bbox=union_bbox(pg.words[i].bbox for i in idx),
                                         direction=detect_direction(text), language=detect_language(text)))
                for t in page.extract_tables() or []:
                    from .models import Cell, Table
                    rows_ = [r for r in t if r]
                    if not rows_:
                        continue
                    cols = max(len(r) for r in rows_)
                    cells = [Cell(row=ri, col=ci, text=(c or "").strip(), isHeader=ri == 0)
                             for ri, r in enumerate(rows_) for ci, c in enumerate(r)]
                    pg.tables.append(Table(rows=len(rows_), cols=cols, cells=cells, confidence=1.0))
                doc.pages.append(pg)
        doc.rebuild_text()
        return doc


PDF_TEXT_INFO = ProviderInfo(
    id="pdf-text", displayName="PDF text layer (no OCR)", version="1.0.0", vendor="Oneofficeautomation",
    tier=1, kind="ocr", capabilities=[Capability.OCR_PRINTED, Capability.TABLES],
    languages=["*"], runsLocally=True, creditCostPerPage={"ocr_printed": 0, "tables": 0},
    supportedMimeTypes=["application/pdf"], optionPack="core",
)
