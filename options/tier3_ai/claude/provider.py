"""Tier 3 option: Claude (Anthropic) for template-free extraction and classification.

Uses the official Anthropic Python SDK (pip install anthropic). The prompt and the
hallucination guard are shared with the OpenAI-compatible connector (every value
must cite OCR line ids); on top of that:

* structured outputs: the reply must match a JSON schema built from the field list,
* page images go with the OCR text (better on scans and phone photos),
* refusal fallbacks ("default"): a declined request is re-run server-side on the
  model Anthropic recommends for that case instead of failing.
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
from pathlib import Path

from docauto.errors import AuthFailed, DocAutoError, ProviderError, ProviderTimeout, RateLimited
from docauto.models import Classification, DocumentResult, ExtractedField, FieldDef, FieldSchema
from docauto.provider import ExtractionProvider, ExtractRequest, HealthStatus

try:
    import anthropic
except ImportError:  # optional dependency: the engine reports "not installed"
    anthropic = None


def _load_shared():
    path = Path(__file__).resolve().parent.parent / "llm_openai_compatible" / "provider.py"
    spec = importlib.util.spec_from_file_location("docauto_option_llm_shared", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_llm = _load_shared()
FALLBACK_BETA = "server-side-fallback-2026-07-01"   # pairs with fallbacks="default"
_SCALAR = {"number": {"type": "number"}, "currency": {"type": "number"}, "boolean": {"type": "boolean"}}


def _nullable(s: dict) -> dict:
    return {"anyOf": [s, {"type": "null"}]}


def _value_schema(f: FieldDef) -> dict:
    if f.type == "table":
        if not f.columns:
            return _nullable({"type": "array", "items": {"type": "string"}})
        cols = {c.name: _nullable(_SCALAR.get(c.type, {"type": "string"})) for c in f.columns}
        return _nullable({"type": "array", "items": {"type": "object", "properties": cols, "required": list(cols),
                                                     "additionalProperties": False}})
    if f.type == "enum" and f.enumValues:
        return _nullable({"type": "string", "enum": list(f.enumValues)})
    return _nullable(_SCALAR.get(f.type, {"type": "string"}))


def response_schema(schema: FieldSchema) -> dict:
    """JSON schema for {"fields": {<name>: {"value", "lines", "confidence"}}} (the shape parse_llm_fields reads)."""
    fields = {f.name: {"type": "object",
                       "properties": {"value": _value_schema(f), "lines": {"type": "array", "items": {"type": "string"}},
                                      "confidence": {"type": "number"}},
                       "required": ["value", "lines", "confidence"], "additionalProperties": False}
              for f in schema.fields}
    return {"type": "object",
            "properties": {"fields": {"type": "object", "properties": fields, "required": list(fields),
                                      "additionalProperties": False}},
            "required": ["fields"], "additionalProperties": False}


class ClaudeExtractionProvider(ExtractionProvider):
    def init(self, ctx):
        super().init(ctx)
        cfg = ctx.config
        self.model = cfg.get("model") or "claude-opus-5-5"
        self.effort = cfg.get("effort") or "medium"
        self.api_key = ctx.credentials.get("apiKey") or ""
        self.send_images = bool(cfg.get("sendImages", True))
        self.max_images = int(cfg.get("maxImages", 5))
        self.max_tokens = int(cfg.get("maxTokens", 16000))
        self.timeout = float(cfg.get("timeoutSeconds", 120))
        self.fallbacks = cfg.get("fallbacks", "default")
        if cfg.get("region"):
            self.info.regions = [cfg["region"]]
        self._client = None

    def health_check(self) -> HealthStatus:
        if anthropic is None:
            return HealthStatus(False, "anthropic not installed: pip install anthropic")
        if not self.api_key and not os.environ.get("ANTHROPIC_API_KEY"):
            return HealthStatus(False, "Anthropic API key not configured")
        return HealthStatus(True, f"model={self.model}")

    def client(self):
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self.api_key or None, timeout=self.timeout)
        return self._client

    def _error(self, e: Exception) -> DocAutoError:
        pid = self.info.id
        if anthropic is not None:
            if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
                return AuthFailed(str(e), pid)
            if isinstance(e, anthropic.RateLimitError):
                return RateLimited(str(e), pid)
            if isinstance(e, anthropic.APITimeoutError):
                return ProviderTimeout(str(e), pid)
            if isinstance(e, anthropic.BadRequestError):
                return DocAutoError(f"request rejected: {e}", pid)      # not retryable
            if isinstance(e, anthropic.APIStatusError):
                return ProviderError(f"HTTP {e.status_code}: {e}", pid)
        return ProviderError(f"{type(e).__name__}: {e}", pid)

    def _call(self, content: list, out_schema: dict, max_tokens: int) -> str:
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=_llm.SYSTEM_PROMPT,
                      output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": out_schema}},
                      messages=[{"role": "user", "content": content}])
        try:
            if self.fallbacks:
                r = self.client().beta.messages.create(betas=[FALLBACK_BETA], extra_body={"fallbacks": self.fallbacks},
                                                       **kwargs)
            else:
                r = self.client().messages.create(**kwargs)
        except Exception as e:
            raise self._error(e) from e
        if r.stop_reason == "refusal":
            category = getattr(r.stop_details, "category", None) if r.stop_details else None
            raise DocAutoError(f"Claude declined the request ({category or 'no category'})", self.info.id)
        if r.stop_reason == "max_tokens":
            raise DocAutoError(f"reply cut off at max_tokens={max_tokens}", self.info.id)
        text = next((b.text for b in r.content if b.type == "text"), None)
        if text is None:
            raise ProviderError("reply has no text block", self.info.id)
        return text

    def _content(self, text: str, images) -> list:
        parts: list[dict] = []
        if self.send_images:
            for img in (images or [])[: self.max_images]:
                buf = io.BytesIO()
                im = img.convert("RGB")
                im.thumbnail((1600, 1600))
                im.save(buf, format="JPEG", quality=85)
                parts.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                          "data": base64.standard_b64encode(buf.getvalue()).decode()}})
        parts.append({"type": "text", "text": text})
        return parts

    def extract(self, req: ExtractRequest) -> list[ExtractedField]:
        prompt, index = _llm.build_prompt(req.document, req.schema)
        if req.instructions:
            prompt = req.instructions + "\n\n" + prompt
        text = self._call(self._content(prompt, req.images), response_schema(req.schema), self.max_tokens)
        try:
            return _llm.parse_llm_fields(text, req.schema, index, req.document, self.info.id)
        except (ValueError, json.JSONDecodeError) as e:
            raise ProviderError(f"invalid JSON from Claude: {e}", self.info.id)

    def classify(self, doc: DocumentResult, labels: list[str]) -> Classification:
        out = {"type": "object", "properties": {"label": {"type": "string", "enum": list(labels) + ["other"]},
                                                "confidence": {"type": "number"}},
               "required": ["label", "confidence"], "additionalProperties": False}
        prompt = (f"Classify this document into exactly one of {json.dumps(labels, ensure_ascii=False)} "
                  f"or \"other\".\n\n{doc.text[:8000]}")
        try:
            d = json.loads(self._call([{"type": "text", "text": prompt}], out, 4000))
        except json.JSONDecodeError as e:
            raise ProviderError(f"invalid classification output: {e}", self.info.id)
        label = d.get("label") if d.get("label") in labels else "other"
        return Classification(label=label, confidence=float(d.get("confidence") or 0.5))
