"""Claude option: request shape (structured output, images, fallbacks), grounding, refusals (fake SDK client)."""
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from conftest import load_option
from docauto.errors import DocAutoError
from docauto.models import BBox, DocumentResult, FieldDef, FieldSchema, Line, Page
from docauto.provider import Capability, ExtractRequest, ProviderContext, ProviderInfo

mod = load_option("tier3_ai/claude/provider.py")

SCHEMA = FieldSchema(name="Invoice", fields=[
    FieldDef(name="invoice_number"), FieldDef(name="total", type="currency"), FieldDef(name="supplier"),
    FieldDef(name="currency", type="enum", enumValues=["SAR", "USD"]),
    FieldDef(name="line_items", type="table", columns=[FieldDef(name="description"),
                                                       FieldDef(name="amount", type="currency")])])


def doc():
    p = Page(pageNumber=1, width=1, height=1, lines=[
        Line(text="Invoice Number: INV-7", bbox=BBox(x=0.1, y=0.1, w=0.4, h=0.02), confidence=0.96),
        Line(text="Total: 1,150.00 SAR", bbox=BBox(x=0.5, y=0.3, w=0.4, h=0.02), confidence=0.9)])
    d = DocumentResult(documentId="d", pages=[p]); d.rebuild_text()
    return d


class FakeClient:
    def __init__(self, reply, stop_reason="end_turn", stop_details=None):
        self.calls, self.reply, self.stop, self.details = [], reply, stop_reason, stop_details
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return SimpleNamespace(stop_reason=self.stop, stop_details=self.details,
                               content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)])


def provider(client, **cfg):
    p = mod.ClaudeExtractionProvider()
    p.info = ProviderInfo(id="claude", displayName="", version="1", vendor="", tier=3, kind="extraction",
                          capabilities=[Capability.EXTRACT_SCHEMA], languages=["*"], runsLocally=False)
    p.init(ProviderContext(config=cfg, credentials={"apiKey": "k"}))
    p._client = client
    return p


def test_response_schema_is_strict_and_typed():
    s = mod.response_schema(SCHEMA)
    fields = s["properties"]["fields"]
    assert fields["additionalProperties"] is False and set(fields["required"]) == {f.name for f in SCHEMA.fields}
    item = fields["properties"]["total"]
    assert item["required"] == ["value", "lines", "confidence"] and item["additionalProperties"] is False
    assert {"type": "number"} in item["properties"]["value"]["anyOf"]
    assert {"type": "string", "enum": ["SAR", "USD"]} in fields["properties"]["currency"]["properties"]["value"]["anyOf"]
    rows = fields["properties"]["line_items"]["properties"]["value"]["anyOf"][0]["items"]
    assert rows["required"] == ["description", "amount"] and rows["additionalProperties"] is False


def test_request_shape_and_grounding():
    reply = {"fields": {"invoice_number": {"value": "INV-7", "lines": ["p1.l0"], "confidence": 0.95},
                        "total": {"value": 1150.0, "lines": ["p1.l1"], "confidence": 0.9},
                        "supplier": {"value": "Imaginary Corp", "lines": [], "confidence": 0.9},
                        "currency": {"value": "SAR", "lines": ["p1.l1"], "confidence": 0.9},
                        "line_items": {"value": None, "lines": [], "confidence": 0}}}
    fc = FakeClient(reply)
    p = provider(fc, model="claude-sonnet-5-5", effort="low")
    out = {f.name: f for f in p.extract(ExtractRequest(document=doc(), schema=SCHEMA,
                                                       images=[Image.new("RGB", (2400, 3200), "white")]))}
    kw = fc.calls[0]
    assert kw["model"] == "claude-sonnet-5-5" and kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["extra_body"] == {"fallbacks": "default"}
    assert "temperature" not in kw and "thinking" not in kw
    content = kw["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    assert content[-1]["type"] == "text" and "[p1.l0] Invoice Number: INV-7" in content[-1]["text"]
    assert out["invoice_number"].value == "INV-7" and out["total"].value == 1150.0 and out["total"].page == 1
    assert "hallucination" in out["supplier"].validation.errors[0]          # shared grounding guard
    assert out["line_items"].value is None


def test_defaults_without_images_or_fallbacks():
    fc = FakeClient({"fields": {}})
    p = provider(fc, sendImages=False, fallbacks="")
    p.extract(ExtractRequest(document=doc(), schema=SCHEMA, images=[Image.new("RGB", (10, 10))]))
    kw = fc.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["output_config"]["effort"] == "medium"
    assert "betas" not in kw and [c["type"] for c in kw["messages"][0]["content"]] == ["text"]


def test_refusal_is_not_retried():
    fc = FakeClient("", stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
    with pytest.raises(DocAutoError) as e:
        provider(fc).extract(ExtractRequest(document=doc(), schema=SCHEMA))
    assert "declined" in e.value.message and "cyber" in e.value.message and not e.value.retryable


def test_classify_uses_label_enum():
    fc = FakeClient({"label": "invoice", "confidence": 0.93})
    c = provider(fc).classify(doc(), ["invoice", "contract"])
    assert c.label == "invoice" and c.confidence == 0.93
    assert fc.calls[0]["output_config"]["format"]["schema"]["properties"]["label"]["enum"] == ["invoice", "contract", "other"]


def test_health_without_sdk_or_key(monkeypatch):
    p = provider(FakeClient({}))
    monkeypatch.setattr(mod, "anthropic", None)
    assert "not installed" in p.health_check().message
    monkeypatch.setattr(mod, "anthropic", SimpleNamespace())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p.api_key = ""
    assert not p.health_check().ok and "API key" in p.health_check().message
