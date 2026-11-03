"""Tier 1 option: PaddleOCR (local, free, stronger on real-world scans).

Install separately:  pip install paddleocr paddlepaddle
PaddleOCR's Python API has changed across major versions (2.x `ocr()` vs 3.x
`predict()`); both result shapes are handled below. Verify against the version
you pin.
"""
from __future__ import annotations

import numpy as np

from docauto.errors import ProviderError
from docauto.models import DocumentResult, Line, Page
from docauto.normalize import detect_direction, polygon_to_bbox
from docauto.provider import HealthStatus, OcrProvider, RecognizeRequest

LANG_MAP = {"en": "en", "ar": "arabic", "fr": "fr", "de": "german", "zh": "ch", "es": "es", "it": "it",
            "pt": "pt", "ru": "ru", "hi": "hi", "ur": "ur", "fa": "fa"}


class PaddleOcrProvider(OcrProvider):
    def health_check(self) -> HealthStatus:
        try:
            import paddleocr  # noqa: F401
            self._engines = {}
            return HealthStatus(True, "paddleocr installed")
        except Exception as e:
            return HealthStatus(False, f"paddleocr not installed: {e}")

    def _engine(self, lang: str):
        from paddleocr import PaddleOCR

        if lang not in self._engines:
            try:
                self._engines[lang] = PaddleOCR(lang=lang, use_angle_cls=True)
            except TypeError:  # 3.x signature
                self._engines[lang] = PaddleOCR(lang=lang)
        return self._engines[lang]

    @staticmethod
    def _iter_lines(raw):
        """Yield (polygon, text, score) for 2.x and 3.x result formats."""
        if raw is None:
            return
        for item in raw:
            if item is None:
                continue
            if isinstance(item, dict) or hasattr(item, "get"):          # 3.x OCRResult
                polys = item.get("rec_polys") or item.get("dt_polys") or []
                for poly, text, sc in zip(polys, item.get("rec_texts", []), item.get("rec_scores", [])):
                    yield [list(map(float, p)) for p in poly], text, float(sc)
            elif isinstance(item, list):                                 # 2.x: [[poly, (text, score)], ...]
                for entry in item:
                    if entry and len(entry) == 2:
                        poly, (text, sc) = entry
                        yield poly, text, float(sc)

    def recognize(self, req: RecognizeRequest) -> DocumentResult:
        lang = LANG_MAP.get((req.languages or ["en"])[0], "en")
        engine = self._engine(lang)
        doc = DocumentResult(documentId=req.document_id)
        for pno, img in enumerate(req.pages, 1):
            W, H = img.size
            arr = np.array(img)
            try:
                raw = engine.predict(arr) if hasattr(engine, "predict") else engine.ocr(arr)
            except Exception as e:
                raise ProviderError(f"paddleocr failed: {e}", self.info.id)
            page = Page(pageNumber=pno, width=W, height=H)
            for poly, text, sc in self._iter_lines(raw):
                page.lines.append(Line(text=text, bbox=polygon_to_bbox(poly, W, H), confidence=round(sc, 4),
                                       direction=detect_direction(text)))
            page.lines.sort(key=lambda l: (round(l.bbox.y, 2), l.bbox.x))
            doc.pages.append(page)
        doc.rebuild_text()
        return doc
