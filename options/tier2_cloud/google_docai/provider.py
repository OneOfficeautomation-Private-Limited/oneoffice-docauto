"""Tier 2 option: Google Cloud Document AI (google-cloud-documentai client).

Uses a Document OCR processor for text and a Form Parser processor for tables
and key-value pairs. Processor IDs are created in the customer's GCP project.
"""
from __future__ import annotations

from typing import Any

from docauto.errors import AuthFailed, InvalidInput, ProviderError, RateLimited
from docauto.models import BBox, Cell, DocumentResult, KeyValue, Line, Page, Table, Word
from docauto.normalize import polygon_to_bbox
from docauto.provider import Capability, HealthStatus, OcrProvider, RecognizeRequest


def _text(layout: dict, full: str) -> str:
    segs = (layout.get("textAnchor") or {}).get("textSegments", [])
    return "".join(full[int(s.get("startIndex", 0)):int(s.get("endIndex", 0))] for s in segs).strip()


def _bb(layout: dict) -> BBox:
    verts = (layout.get("boundingPoly") or {}).get("normalizedVertices") or []
    if not verts:
        return BBox(x=0, y=0, w=0, h=0)
    return polygon_to_bbox([(v.get("x", 0.0), v.get("y", 0.0)) for v in verts])


def map_docai(document: dict[str, Any], document_id: str) -> DocumentResult:
    """Map a Document AI `Document` (as camelCase dict, e.g. MessageToDict) to DocumentResult."""
    full = document.get("text", "")
    doc = DocumentResult(documentId=document_id, text=full)
    for i, p in enumerate(document.get("pages", []), 1):
        dim = p.get("dimension", {})
        page = Page(pageNumber=int(p.get("pageNumber", i)), width=float(dim.get("width", 1)),
                    height=float(dim.get("height", 1)))
        for t in p.get("tokens", []):
            lay = t.get("layout", {})
            page.words.append(Word(text=_text(lay, full), bbox=_bb(lay), confidence=lay.get("confidence")))
        for ln in p.get("lines", []):
            lay = ln.get("layout", {})
            page.lines.append(Line(text=_text(lay, full), bbox=_bb(lay), confidence=lay.get("confidence")))
        for t in p.get("tables", []):
            cells, r = [], 0
            for section, header in (("headerRows", True), ("bodyRows", False)):
                for row in t.get(section, []):
                    c_idx = 0
                    for c in row.get("cells", []):
                        lay = c.get("layout", {})
                        cells.append(Cell(row=r, col=c_idx, rowSpan=int(c.get("rowSpan", 1)),
                                          colSpan=int(c.get("colSpan", 1)), text=_text(lay, full),
                                          isHeader=header, bbox=_bb(lay), confidence=lay.get("confidence")))
                        c_idx += int(c.get("colSpan", 1))
                    r += 1
            if cells:
                page.tables.append(Table(rows=r, cols=max(c.col + c.colSpan for c in cells), cells=cells,
                                         bbox=_bb(t.get("layout", {}))))
        for ff in p.get("formFields", []):
            k, v = ff.get("fieldName", {}), ff.get("fieldValue", {})
            page.keyValuePairs.append(KeyValue(key=_text(k, full).rstrip(":"), value=_text(v, full),
                                               keyBbox=_bb(k), valueBbox=_bb(v), confidence=v.get("confidence")))
        doc.pages.append(page)
    # specialised processors (invoice etc.) return entities -> expose as key/values
    for e in document.get("entities", []):
        anchor = e.get("pageAnchor", {}).get("pageRefs", [{}])[0]
        pno = int(anchor.get("page", 0)) + 1
        if 1 <= pno <= len(doc.pages):
            doc.pages[pno - 1].keyValuePairs.append(KeyValue(key=e.get("type", ""), value=e.get("mentionText", ""),
                                                             confidence=e.get("confidence")))
    return doc


class GoogleDocumentAiProvider(OcrProvider):
    def init(self, ctx):
        super().init(ctx)
        cfg = ctx.config
        self.project = cfg.get("projectId")
        self.location = cfg.get("location", "us")
        self.ocr_processor = cfg.get("ocrProcessorId")
        self.form_processor = cfg.get("formProcessorId")
        self.info.regions = [self.location]

    def health_check(self) -> HealthStatus:
        try:
            from google.cloud import documentai  # noqa: F401
        except Exception:
            return HealthStatus(False, "google-cloud-documentai not installed")
        if not (self.project and self.ocr_processor):
            return HealthStatus(False, "projectId/ocrProcessorId not configured")
        return HealthStatus(True, "configured")

    def recognize(self, req: RecognizeRequest) -> DocumentResult:
        from google.api_core.client_options import ClientOptions
        from google.cloud import documentai
        from google.protobuf.json_format import MessageToDict

        creds_file = self.ctx.credentials.get("serviceAccountFile")
        opts = ClientOptions(api_endpoint=f"{self.location}-documentai.googleapis.com")
        client = (documentai.DocumentProcessorServiceClient.from_service_account_file(creds_file, client_options=opts)
                  if creds_file else documentai.DocumentProcessorServiceClient(client_options=opts))
        rich = {Capability.TABLES, Capability.KEY_VALUE} & set(req.capabilities)
        processor = self.form_processor if rich and self.form_processor else self.ocr_processor
        name = client.processor_path(self.project, self.location, processor)
        if not req.source_bytes:
            raise InvalidInput("google-docai needs the original file bytes", self.info.id)
        try:
            result = client.process_document(request=documentai.ProcessRequest(
                name=name, raw_document=documentai.RawDocument(content=req.source_bytes,
                                                               mime_type=req.mime_type or "application/pdf")))
        except Exception as e:
            s = str(e)
            if "PERMISSION_DENIED" in s or "UNAUTHENTICATED" in s:
                raise AuthFailed(s, self.info.id)
            if "RESOURCE_EXHAUSTED" in s:
                raise RateLimited(s, self.info.id)
            if "INVALID_ARGUMENT" in s:
                raise InvalidInput(s, self.info.id)
            raise ProviderError(s, self.info.id)
        as_dict = MessageToDict(documentai.Document.pb(result.document))
        return map_docai(as_dict, req.document_id)
