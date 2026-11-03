"""Tier 1 option: Tesseract 5 (local, free). Default for screen OCR and offline documents."""
from __future__ import annotations

import io

from docauto.errors import ProviderError, ProviderUnavailable
from docauto.models import DocumentResult, Line, Page, Word
from docauto.normalize import bbox_from_pixels, conf100, detect_direction, union_bbox
from docauto.provider import HealthStatus, OcrProvider, RecognizeRequest

LANG_MAP = {"en": "eng", "ar": "ara", "fr": "fra", "de": "deu", "hi": "hin", "ur": "urd", "fa": "fas",
            "zh": "chi_sim", "es": "spa", "it": "ita", "pt": "por", "ru": "rus", "tr": "tur"}


def tess_langs(langs: list[str]) -> str:
    return "+".join(dict.fromkeys(LANG_MAP.get(l, l) for l in (langs or ["en"])))


def _merge_rows(page: Page, max_gap: float = 0.25) -> list[Line]:
    """Join lines that sit on the same visual row and are reasonably close horizontally."""
    lines = sorted(page.lines, key=lambda l: (l.bbox.y, l.bbox.x))
    rows: list[list[Line]] = []
    for ln in lines:
        cy = ln.bbox.y + ln.bbox.h / 2
        for row in rows:
            ref = row[-1]
            rcy = ref.bbox.y + ref.bbox.h / 2
            gap = ln.bbox.x - (ref.bbox.x + ref.bbox.w)
            if abs(cy - rcy) < max(ref.bbox.h, ln.bbox.h) * 0.5 and -0.01 < gap < max_gap:
                row.append(ln)
                break
        else:
            rows.append([ln])
    out = []
    for row in rows:
        row.sort(key=lambda l: l.bbox.x)
        if len(row) == 1:
            out.append(row[0])
            continue
        confs = [l.confidence for l in row if l.confidence is not None]
        text = " ".join(l.text for l in row)
        out.append(Line(text=text, bbox=union_bbox(l.bbox for l in row),
                        confidence=round(sum(confs) / len(confs), 4) if confs else None,
                        direction=detect_direction(text), wordIndexes=[i for l in row for i in l.wordIndexes]))
    return sorted(out, key=lambda l: (l.bbox.y, l.bbox.x))


class TesseractProvider(OcrProvider):
    def health_check(self) -> HealthStatus:
        try:
            import os
            import shutil
            import pytesseract
            cmd = self.ctx.config.get("tesseractCmd") or os.environ.get("TESSERACT_CMD")
            if not cmd and not shutil.which("tesseract"):
                default_win = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
                cmd = default_win if os.path.exists(default_win) else None
            if cmd:
                pytesseract.pytesseract.tesseract_cmd = cmd
            v = pytesseract.get_tesseract_version()
            self.installed_langs = set(pytesseract.get_languages(config=""))
            return HealthStatus(True, f"tesseract {v}; langs: {len(self.installed_langs)}")
        except Exception as e:
            return HealthStatus(False, f"tesseract not available: {e}")

    def _check_langs(self, langs: list[str]) -> str:
        code = tess_langs(langs)
        missing = [c for c in code.split("+") if getattr(self, "installed_langs", None) and c not in self.installed_langs]
        if missing:
            raise ProviderUnavailable(f"tesseract language data missing: {missing}", self.info.id)
        return code

    def recognize(self, req: RecognizeRequest) -> DocumentResult:
        import pytesseract
        from pytesseract import Output

        lang = self._check_langs(req.languages)
        psm = self.ctx.config.get("psm", 11 if "screen_ocr" in [c.value for c in req.capabilities] else 3)
        doc = DocumentResult(documentId=req.document_id)
        for pno, img in enumerate(req.pages, 1):
            W, H = img.size
            try:
                d = pytesseract.image_to_data(img, lang=lang, config=f"--psm {psm}", output_type=Output.DICT)
            except Exception as e:
                raise ProviderError(f"tesseract failed: {e}", self.info.id)
            page = Page(pageNumber=pno, width=W, height=H, unit="pixel")
            groups: dict[tuple, list[int]] = {}
            for i, txt in enumerate(d["text"]):
                if not str(txt).strip():
                    continue
                key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
                groups.setdefault(key, []).append(len(page.words))
                page.words.append(Word(text=str(txt),
                                       bbox=bbox_from_pixels(d["left"][i], d["top"][i], d["width"][i],
                                                             d["height"][i], W, H),
                                       confidence=conf100(float(d["conf"][i]))))
            for key in sorted(groups, key=lambda k: (min(page.words[i].bbox.y for i in groups[k]), k)):
                idx = groups[key]
                text = " ".join(page.words[i].text for i in idx)
                direction = detect_direction(text)
                if direction == "rtl":  # tesseract returns RTL words left-to-right by position
                    text = " ".join(page.words[i].text for i in sorted(idx, key=lambda i: -page.words[i].bbox.x))
                confs = [page.words[i].confidence for i in idx if page.words[i].confidence is not None]
                page.lines.append(Line(text=text, bbox=union_bbox(page.words[i].bbox for i in idx),
                                       confidence=round(sum(confs) / len(confs), 4) if confs else None,
                                       direction=direction, wordIndexes=idx))
            if psm == 11:  # sparse mode splits "Label:   Value" into separate lines -> re-join rows
                page.lines = _merge_rows(page)
            doc.pages.append(page)
        doc.rebuild_text()
        return doc

    def searchable_pdf(self, images, languages) -> bytes:
        import pypdfium2 as pdfium
        import pytesseract

        lang = self._check_langs(languages)
        out = pdfium.PdfDocument.new()
        for img in images:
            part = pytesseract.image_to_pdf_or_hocr(img, lang=lang, extension="pdf")
            src = pdfium.PdfDocument(part)
            out.import_pages(src)
        buf = io.BytesIO()
        out.save(buf)
        return buf.getvalue()
