"""Tier 2 option: Amazon Textract (boto3).

Uses the synchronous API page-by-page (each page rendered to PNG), which avoids
needing an S3 bucket. For very large batches switch to the async
StartDocumentAnalysis API with S3 input.
"""
from __future__ import annotations

import io
from typing import Any

from docauto.errors import AuthFailed, InvalidInput, ProviderError, ProviderUnavailable, RateLimited
from docauto.models import BBox, Cell, DocumentResult, KeyValue, Line, Page, Table, Word
from docauto.normalize import conf100
from docauto.provider import Capability, HealthStatus, OcrProvider, RecognizeRequest


def _bb(block: dict) -> BBox:
    b = block.get("Geometry", {}).get("BoundingBox", {})  # already 0-1
    return BBox(x=b.get("Left", 0.0), y=b.get("Top", 0.0), w=b.get("Width", 0.0), h=b.get("Height", 0.0))


def _children(block: dict, rel_type: str = "CHILD") -> list[str]:
    return [i for r in block.get("Relationships", []) if r["Type"] == rel_type for i in r["Ids"]]


def map_textract_page(blocks: list[dict[str, Any]], page_number: int, width: float, height: float) -> Page:
    """Map Textract Blocks for ONE page to a Page (pure function, unit-tested)."""
    by_id = {b["Id"]: b for b in blocks}
    page = Page(pageNumber=page_number, width=width, height=height)
    word_index: dict[str, int] = {}
    for b in blocks:
        if b["BlockType"] == "WORD":
            word_index[b["Id"]] = len(page.words)
            page.words.append(Word(text=b.get("Text", ""), bbox=_bb(b), confidence=conf100(b.get("Confidence"))))
    for b in blocks:
        if b["BlockType"] == "LINE":
            idx = [word_index[c] for c in _children(b) if c in word_index]
            page.lines.append(Line(text=b.get("Text", ""), bbox=_bb(b), confidence=conf100(b.get("Confidence")),
                                   wordIndexes=idx,
                                   handwritten=any(by_id[c].get("TextType") == "HANDWRITING"
                                                   for c in _children(b) if c in by_id)))

    def text_of(block: dict) -> str:
        parts = []
        for cid in _children(block):
            c = by_id.get(cid, {})
            if c.get("BlockType") == "WORD":
                parts.append(c.get("Text", ""))
            elif c.get("BlockType") == "SELECTION_ELEMENT":
                parts.append("[X]" if c.get("SelectionStatus") == "SELECTED" else "[ ]")
        return " ".join(parts)

    for b in blocks:
        if b["BlockType"] == "TABLE":
            cells = []
            for cid in _children(b):
                c = by_id.get(cid)
                if not c or c["BlockType"] != "CELL":
                    continue
                cells.append(Cell(row=c["RowIndex"] - 1, col=c["ColumnIndex"] - 1, rowSpan=c.get("RowSpan", 1),
                                  colSpan=c.get("ColumnSpan", 1), text=text_of(c), bbox=_bb(c),
                                  confidence=conf100(c.get("Confidence")),
                                  isHeader="COLUMN_HEADER" in c.get("EntityTypes", [])))
            if cells:
                page.tables.append(Table(rows=max(c.row + c.rowSpan for c in cells),
                                         cols=max(c.col + c.colSpan for c in cells), cells=cells, bbox=_bb(b),
                                         confidence=conf100(b.get("Confidence"))))
        elif b["BlockType"] == "KEY_VALUE_SET" and "KEY" in b.get("EntityTypes", []):
            vals = [by_id[v] for v in _children(b, "VALUE") if v in by_id]
            page.keyValuePairs.append(KeyValue(
                key=text_of(b).rstrip(":").strip(), value=" ".join(text_of(v) for v in vals).strip(),
                keyBbox=_bb(b), valueBbox=_bb(vals[0]) if vals else None,
                confidence=conf100(min([b.get("Confidence", 0)] + [v.get("Confidence", 0) for v in vals]))))
    return page


class TextractProvider(OcrProvider):
    def init(self, ctx):
        super().init(ctx)
        self.region = ctx.config.get("region")
        if self.region:
            self.info.regions = [self.region]

    def health_check(self) -> HealthStatus:
        try:
            import boto3  # noqa: F401
        except Exception:
            return HealthStatus(False, "boto3 not installed (pip install boto3)")
        if not self.region:
            return HealthStatus(False, "region not configured")
        return HealthStatus(True, "configured")

    def _client(self):
        import boto3

        creds = self.ctx.credentials
        kwargs = {"region_name": self.region}
        if creds.get("accessKeyId"):
            kwargs.update(aws_access_key_id=creds["accessKeyId"], aws_secret_access_key=creds.get("secretAccessKey"),
                          aws_session_token=creds.get("sessionToken") or None)
        return boto3.client("textract", **kwargs)

    def recognize(self, req: RecognizeRequest) -> DocumentResult:
        client = self._client()
        features = []
        if Capability.TABLES in req.capabilities:
            features.append("TABLES")
        if Capability.KEY_VALUE in req.capabilities or Capability.SELECTION_MARKS in req.capabilities:
            features.append("FORMS")
        doc = DocumentResult(documentId=req.document_id)
        for pno, img in enumerate(req.pages, 1):
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            payload = {"Document": {"Bytes": buf.getvalue()}}
            try:
                resp = (client.analyze_document(FeatureTypes=features, **payload) if features
                        else client.detect_document_text(**payload))
            except Exception as e:  # botocore ClientError
                code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
                if code in ("AccessDeniedException", "UnrecognizedClientException", "InvalidSignatureException"):
                    raise AuthFailed(str(e), self.info.id)
                if code in ("ThrottlingException", "ProvisionedThroughputExceededException"):
                    raise RateLimited(str(e), self.info.id)
                if code in ("UnsupportedDocumentException", "BadDocumentException", "DocumentTooLargeException"):
                    raise InvalidInput(str(e), self.info.id)
                if code == "":
                    raise ProviderUnavailable(str(e), self.info.id)
                raise ProviderError(str(e), self.info.id)
            doc.pages.append(map_textract_page(resp.get("Blocks", []), pno, *img.size))
        doc.rebuild_text()
        return doc
