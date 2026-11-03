"""Tier 2 option: Azure AI Document Intelligence (REST, no SDK dependency).

Verify the API version and paths against current Microsoft docs when deploying.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from docauto.errors import AuthFailed, InvalidInput, ProviderError, ProviderTimeout, ProviderUnavailable, \
    QuotaExceeded, RateLimited
from docauto.models import BBox, Block, Cell, DocumentResult, KeyValue, Line, Page, Table, Word
from docauto.normalize import flat_polygon_to_points, polygon_to_bbox
from docauto.provider import Capability, HealthStatus, OcrProvider, RecognizeRequest

ROLE_MAP = {"title": "title", "sectionHeading": "title", "pageHeader": "header", "pageFooter": "footer",
            "pageNumber": "footer"}


def _bbox(polygon: list[float] | None, W: float, H: float) -> BBox | None:
    if not polygon:
        return None
    return polygon_to_bbox(flat_polygon_to_points(polygon), W, H)


def _region_bbox(regions: list[dict] | None, sizes: dict[int, tuple[float, float]]):
    if not regions:
        return None, None
    r = regions[0]
    W, H = sizes.get(r["pageNumber"], (1.0, 1.0))
    return r["pageNumber"], _bbox(r.get("polygon"), W, H)


def map_azure(result: dict[str, Any], document_id: str) -> DocumentResult:
    """Map Azure `analyzeResult` JSON to DocumentResult (pure function, unit-tested)."""
    doc = DocumentResult(documentId=document_id)
    sizes: dict[int, tuple[float, float]] = {}
    pages: dict[int, Page] = {}
    for p in result.get("pages", []):
        W, H = float(p["width"]), float(p["height"])
        sizes[p["pageNumber"]] = (W, H)
        page = Page(pageNumber=p["pageNumber"], width=W, height=H,
                    unit="inch" if p.get("unit") == "inch" else "pixel", rotation=float(p.get("angle") or 0))
        words = p.get("words", [])
        for w in words:
            page.words.append(Word(text=w["content"], bbox=_bbox(w.get("polygon"), W, H) or BBox(x=0, y=0, w=0, h=0),
                                   confidence=w.get("confidence")))
        for ln in p.get("lines", []):
            spans = ln.get("spans", [])
            idx = []
            for s in spans:
                lo, hi = s["offset"], s["offset"] + s["length"]
                idx += [i for i, w in enumerate(words) if lo <= w.get("span", {}).get("offset", -1) < hi]
            confs = [words[i].get("confidence") for i in idx if words[i].get("confidence") is not None]
            page.lines.append(Line(text=ln["content"], bbox=_bbox(ln.get("polygon"), W, H) or BBox(x=0, y=0, w=0, h=0),
                                   confidence=round(sum(confs) / len(confs), 4) if confs else None,
                                   wordIndexes=idx))
        pages[p["pageNumber"]] = page

    for para in result.get("paragraphs", []) or []:
        pno, bb = _region_bbox(para.get("boundingRegions"), sizes)
        if pno in pages:
            pages[pno].blocks.append(Block(type=ROLE_MAP.get(para.get("role", ""), "paragraph"),
                                           text=para.get("content", ""), bbox=bb))

    for t in result.get("tables", []) or []:
        pno, tb = _region_bbox(t.get("boundingRegions"), sizes)
        cells = []
        for c in t.get("cells", []):
            _, cb = _region_bbox(c.get("boundingRegions"), sizes)
            cells.append(Cell(row=c["rowIndex"], col=c["columnIndex"], rowSpan=c.get("rowSpan", 1),
                              colSpan=c.get("columnSpan", 1), text=c.get("content", ""),
                              isHeader=c.get("kind") in ("columnHeader", "rowHeader"), bbox=cb))
        if pno in pages:
            pages[pno].tables.append(Table(rows=t["rowCount"], cols=t["columnCount"], cells=cells, bbox=tb))

    for kv in result.get("keyValuePairs", []) or []:
        key, val = kv.get("key") or {}, kv.get("value") or {}
        pno, kb = _region_bbox(key.get("boundingRegions"), sizes)
        _, vb = _region_bbox(val.get("boundingRegions"), sizes)
        if pno in pages:
            pages[pno].keyValuePairs.append(KeyValue(key=key.get("content", ""), value=val.get("content", ""),
                                                     keyBbox=kb, valueBbox=vb, confidence=kv.get("confidence")))

    # prebuilt model fields -> key/value pairs (extractors pick them up by label)
    for d in result.get("documents", []) or []:
        for name, f in (d.get("fields") or {}).items():
            if not isinstance(f, dict) or "content" not in f:
                continue
            pno, vb = _region_bbox(f.get("boundingRegions"), sizes)
            if pno in pages:
                pages[pno].keyValuePairs.append(KeyValue(key=name, value=f["content"], valueBbox=vb,
                                                         confidence=f.get("confidence")))

    doc.pages = [pages[k] for k in sorted(pages)]
    doc.text = result.get("content", "") or ""
    if not doc.text:
        doc.rebuild_text()
    return doc


class AzureDocumentIntelligenceProvider(OcrProvider):
    def init(self, ctx):
        super().init(ctx)
        cfg = ctx.config
        self.endpoint = (cfg.get("endpoint") or "").rstrip("/")
        self.api_version = cfg.get("apiVersion", "2024-11-30")
        self.key = ctx.credentials.get("apiKey", "")
        if cfg.get("region"):
            self.info.regions = [cfg["region"]]
        if cfg.get("runsLocally"):  # on-prem container
            self.info.runsLocally = True
        self.timeout_s = float(cfg.get("timeoutSeconds", 120))

    def health_check(self) -> HealthStatus:
        if not self.endpoint or not self.key:
            return HealthStatus(False, "endpoint/apiKey not configured")
        return HealthStatus(True, "configured")

    def _model(self, req: RecognizeRequest) -> str:
        cfg = self.ctx.config
        if req.options.get("prebuiltModel"):
            return req.options["prebuiltModel"]
        rich = {Capability.TABLES, Capability.KEY_VALUE, Capability.LAYOUT, Capability.SELECTION_MARKS}
        if rich & set(req.capabilities):
            return cfg.get("layoutModel", "prebuilt-layout")
        return cfg.get("readModel", "prebuilt-read")

    def recognize(self, req: RecognizeRequest) -> DocumentResult:
        try:
            return self._recognize(req)
        except httpx.TimeoutException as e:
            raise ProviderTimeout(f"azure timeout: {e}", self.info.id)
        except httpx.HTTPError as e:  # DNS, proxy, TLS, connection refused...
            raise ProviderUnavailable(f"cannot reach Azure endpoint: {e}", self.info.id)

    def _recognize(self, req: RecognizeRequest) -> DocumentResult:
        if not req.source_bytes:
            raise InvalidInput("azure-di needs the original file bytes", self.info.id)
        model = self._model(req)
        url = f"{self.endpoint}/documentintelligence/documentModels/{model}:analyze"
        params: dict[str, str] = {"api-version": self.api_version}
        if Capability.KEY_VALUE in req.capabilities:
            params["features"] = "keyValuePairs"
        if req.options.get("pages"):
            params["pages"] = ",".join(str(p) for p in req.options["pages"])
        headers = {"Ocp-Apim-Subscription-Key": self.key, "Content-Type": "application/octet-stream"}
        with httpx.Client(timeout=30) as client:
            r = client.post(url, params=params, headers=headers, content=req.source_bytes)
            self._raise(r)
            op = r.headers.get("operation-location")
            if not op:
                raise ProviderError("missing operation-location header", self.info.id)
            deadline = time.time() + self.timeout_s
            while time.time() < deadline:
                time.sleep(float(r.headers.get("retry-after", 1)))
                r = client.get(op, headers={"Ocp-Apim-Subscription-Key": self.key})
                self._raise(r)
                body = r.json()
                status = body.get("status")
                if status == "succeeded":
                    doc = map_azure(body.get("analyzeResult", {}), req.document_id)
                    doc.raw = body
                    return doc
                if status == "failed":
                    raise ProviderError(f"analysis failed: {body.get('error')}", self.info.id)
        raise ProviderTimeout("azure analysis timed out", self.info.id)

    def _raise(self, r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        msg = r.text[:300]
        if r.status_code in (401, 403):
            raise AuthFailed(msg, self.info.id)
        if r.status_code == 429:
            raise RateLimited(msg, self.info.id)
        if r.status_code == 400:
            raise InvalidInput(msg, self.info.id)
        if r.status_code == 402:
            raise QuotaExceeded(msg, self.info.id)
        if r.status_code == 404:
            raise ProviderUnavailable(f"model/endpoint not found: {msg}", self.info.id)
        raise ProviderError(f"HTTP {r.status_code}: {msg}", self.info.id)
