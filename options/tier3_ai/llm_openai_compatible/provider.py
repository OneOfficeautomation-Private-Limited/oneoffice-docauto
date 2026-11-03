"""Tier 3 option: template-free extraction and classification with an LLM.

Works with any OpenAI-compatible /chat/completions endpoint: frontier APIs,
LLM gateways, or self-hosted servers (vLLM, Ollama, LM Studio...). The same
code backs two catalog entries: `llm-cloud` (outbound) and `llm-local` (on-prem).

Hallucination guard: every value must cite OCR line ids; values not found in the
OCR text get their confidence cut so the Validation Station reviews them.
"""
from __future__ import annotations

import base64
import io
import json
import re
from typing import Any

import httpx

from docauto.errors import AuthFailed, ProviderError, ProviderTimeout, RateLimited
from docauto.extraction import coerce
from docauto.models import Classification, DocumentResult, ExtractedField, FieldSchema
from docauto.normalize import union_bbox
from docauto.provider import ExtractionProvider, ExtractRequest, HealthStatus

SYSTEM_PROMPT = (
    "You extract data from business documents (English and Arabic). "
    "Use ONLY information present in the document. Never guess. "
    "If a field is absent, return null. Copy values exactly as printed, then normalize: "
    "dates to YYYY-MM-DD, numbers without currency symbols or thousands separators. "
    "Reply with a single JSON object and nothing else."
)


def build_prompt(doc: DocumentResult, schema: FieldSchema, max_chars: int = 60000) -> tuple[str, dict[str, Any]]:
    """Return (user prompt, line index) where line index maps 'p1.l3' -> (page, line)."""
    index: dict[str, Any] = {}
    rows = []
    for p in doc.pages:
        for i, l in enumerate(p.lines):
            lid = f"p{p.pageNumber}.l{i}"
            index[lid] = (p, l)
            rows.append(f"[{lid}] {l.text}")
    ocr = "\n".join(rows)[:max_chars]
    fields = []
    for f in schema.fields:
        d = {"name": f.name, "type": f.type}
        for k in ("label", "description", "format"):
            if getattr(f, k):
                d[k] = getattr(f, k)
        if f.enumValues:
            d["allowed"] = f.enumValues
        if f.aliases:
            d["aliases"] = f.aliases
        if f.columns:
            d["columns"] = [{"name": c.name, "type": c.type, **({"label": c.label} if c.label else {})}
                            for c in f.columns]
        fields.append(d)
    shape = ('{"fields": {"<name>": {"value": <value or null>, "lines": ["p1.l3", ...], '
             '"confidence": <0..1>}}}  For type "table" value is a list of row objects keyed by column name.')
    prompt = (f"Document type: {schema.name}\n"
              f"{'Description: ' + schema.description if schema.description else ''}\n"
              f"Fields to extract:\n{json.dumps(fields, ensure_ascii=False)}\n\n"
              f"Return JSON shaped as: {shape}\n\n"
              f"OCR text with line ids:\n{ocr}")
    return prompt, index


def _norm(s: str) -> str:
    return re.sub(r"[\s,]+", "", str(s)).lower()


def parse_llm_fields(content: str, schema: FieldSchema, index: dict[str, Any], doc: DocumentResult,
                     provider_id: str) -> list[ExtractedField]:
    """Turn the model's JSON into grounded ExtractedFields (pure function, unit-tested)."""
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    data = json.loads(m.group(0))
    got = data.get("fields", data)
    full = _norm(doc.text)
    out = []
    for f in schema.fields:
        item = got.get(f.name)
        if not isinstance(item, dict):
            item = {"value": item, "lines": [], "confidence": None}
        value = item.get("value")
        lines = [lid for lid in item.get("lines") or [] if lid in index]
        model_conf = item.get("confidence")
        try:
            model_conf = max(0.0, min(1.0, float(model_conf))) if model_conf is not None else 0.7
        except (TypeError, ValueError):
            model_conf = 0.7
        if value in (None, "", []):
            out.append(ExtractedField(name=f.name, type=f.type, value=None, confidence=0.0, providerId=provider_id))
            continue
        raw = value
        if f.type not in ("table", "object") and not isinstance(value, (int, float, bool)):
            value, ok = coerce(str(value), f.type, f.format)
            if not ok:
                model_conf *= 0.5
        # grounding
        ocr_confs = [index[l][1].confidence for l in lines if index[l][1].confidence is not None]
        ocr_conf = sum(ocr_confs) / len(ocr_confs) if ocr_confs else 0.9
        grounded = bool(lines)
        if f.type not in ("table", "object"):
            grounded = grounded and (_norm(raw) in full or _norm(value) in full or
                                     any(_norm(raw) in _norm(index[l][1].text) for l in lines))
        conf = model_conf * ocr_conf * (1.0 if grounded else 0.5)
        page = index[lines[0]][0].pageNumber if lines else None
        bbox = union_bbox(index[l][1].bbox for l in lines if index[l][0].pageNumber == page) if lines else None
        fld = ExtractedField(name=f.name, type=f.type, value=value, rawText=str(raw)[:500],
                             confidence=round(conf, 4), page=page, bbox=bbox, providerId=provider_id)
        if not grounded:
            fld.validation.errors.append("value not found in OCR text (possible hallucination)")
            fld.validation.passed = False
        out.append(fld)
    return out


class LlmExtractionProvider(ExtractionProvider):
    def init(self, ctx):
        super().init(ctx)
        cfg = ctx.config
        self.base_url = (cfg.get("baseUrl") or "").rstrip("/")
        self.model = cfg.get("model")
        self.api_key = ctx.credentials.get("apiKey", "")
        self.send_images = bool(cfg.get("sendImages", False))
        self.temperature = float(cfg.get("temperature", 0))
        self.timeout = float(cfg.get("timeoutSeconds", 120))
        self.max_images = int(cfg.get("maxImages", 3))
        if cfg.get("region"):
            self.info.regions = [cfg["region"]]

    def health_check(self) -> HealthStatus:
        if not self.base_url or not self.model:
            return HealthStatus(False, "baseUrl/model not configured")
        return HealthStatus(True, f"model={self.model}")

    def _chat(self, user_content: Any) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "temperature": self.temperature,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": user_content}]}
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout)
        except httpx.TimeoutException as e:
            raise ProviderTimeout(str(e), self.info.id)
        except httpx.HTTPError as e:
            raise ProviderError(str(e), self.info.id)
        if r.status_code in (401, 403):
            raise AuthFailed(r.text[:300], self.info.id)
        if r.status_code == 429:
            raise RateLimited(r.text[:300], self.info.id)
        if r.status_code >= 400:
            raise ProviderError(f"HTTP {r.status_code}: {r.text[:300]}", self.info.id)
        return r.json()["choices"][0]["message"]["content"]

    def _content(self, text: str, images) -> Any:
        if not (self.send_images and images):
            return text
        parts: list[dict] = [{"type": "text", "text": text}]
        for img in images[: self.max_images]:
            buf = io.BytesIO()
            im = img.copy()
            im.thumbnail((1600, 1600))
            im.save(buf, format="JPEG", quality=85)
            parts.append({"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}})
        return parts

    def extract(self, req: ExtractRequest) -> list[ExtractedField]:
        prompt, index = build_prompt(req.document, req.schema)
        if req.instructions:
            prompt = req.instructions + "\n\n" + prompt
        content = self._chat(self._content(prompt, req.images))
        try:
            return parse_llm_fields(content, req.schema, index, req.document, self.info.id)
        except (ValueError, json.JSONDecodeError):
            repair = self._chat(f"Your previous reply was not valid JSON. Reply again with ONLY the JSON object.\n\n"
                                f"{prompt}")
            try:
                return parse_llm_fields(repair, req.schema, index, req.document, self.info.id)
            except (ValueError, json.JSONDecodeError) as e:
                raise ProviderError(f"model returned invalid JSON twice: {e}", self.info.id)

    def classify(self, doc: DocumentResult, labels: list[str]) -> Classification:
        text = doc.text[:8000]
        content = self._chat(f"Classify this document into exactly one of {json.dumps(labels, ensure_ascii=False)} "
                             f"or \"other\". Reply as {{\"label\": \"...\", \"confidence\": 0..1}}.\n\n{text}")
        try:
            d = json.loads(re.search(r"\{.*\}", content, re.S).group(0))
        except Exception as e:
            raise ProviderError(f"invalid classification output: {e}", self.info.id)
        label = d.get("label") if d.get("label") in labels else "other"
        return Classification(label=label, confidence=float(d.get("confidence") or 0.5))
