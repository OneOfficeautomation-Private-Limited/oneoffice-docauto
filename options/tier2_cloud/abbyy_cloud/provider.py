"""Tier 2 option: ABBYY connector TEMPLATE.

ABBYY sells several products with different integration styles:
  * ABBYY Vantage            - cloud/on-prem IDP platform with a REST API (skills, transactions)
  * ABBYY FineReader Engine  - on-prem OCR SDK, embedded under an OEM licence
  * ABBYY Cloud OCR SDK      - hosted OCR API (check current availability)

Which one you integrate depends on your partner/OEM agreement, so this file is
deliberately a template: implement `call_engine()` for your product and map its
output with `map_abbyy_words()` (or write a richer mapper). Everything else
(policy, routing, metering, validation) already works once recognize() returns
a DocumentResult.
"""
from __future__ import annotations

from typing import Any, Iterable

from docauto.errors import ProviderUnavailable
from docauto.models import DocumentResult, Line, Page
from docauto.normalize import bbox_from_pixels, detect_direction
from docauto.provider import HealthStatus, OcrProvider, RecognizeRequest


def map_abbyy_words(lines: Iterable[dict[str, Any]], page_number: int, width: float, height: float) -> Page:
    """Generic mapper: lines as {text, left, top, right, bottom, confidence(0-100)}."""
    page = Page(pageNumber=page_number, width=width, height=height)
    for ln in lines:
        conf = ln.get("confidence")
        page.lines.append(Line(text=ln["text"],
                               bbox=bbox_from_pixels(ln["left"], ln["top"], ln["right"] - ln["left"],
                                                     ln["bottom"] - ln["top"], width, height),
                               confidence=None if conf is None else conf / 100.0,
                               direction=detect_direction(ln["text"])))
    return page


class AbbyyProvider(OcrProvider):
    def init(self, ctx):
        super().init(ctx)
        self.product = ctx.config.get("product")  # "vantage" | "fre" | "cloud"
        if ctx.config.get("region"):
            self.info.regions = [ctx.config["region"]]
        if self.product == "fre":
            self.info.runsLocally = True  # on-prem SDK never leaves the network

    def health_check(self) -> HealthStatus:
        if not self.product:
            return HealthStatus(False, "template connector: set config.product and implement call_engine()")
        return HealthStatus(True, f"product={self.product}")

    def call_engine(self, req: RecognizeRequest) -> list[Page]:
        raise ProviderUnavailable(
            f"ABBYY '{self.product}' integration not implemented yet; complete call_engine() "
            "against the ABBYY API/SDK covered by your agreement", self.info.id)

    def recognize(self, req: RecognizeRequest) -> DocumentResult:
        doc = DocumentResult(documentId=req.document_id, pages=self.call_engine(req))
        doc.rebuild_text()
        return doc
