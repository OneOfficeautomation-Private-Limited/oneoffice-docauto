"""Tier 3 AI option: prompt building, grounding/hallucination guard, HTTP handling (mocked)."""
import json

import httpx
import pytest

from conftest import load_option
from docauto.errors import AuthFailed
from docauto.models import BBox, DocumentResult, FieldDef, FieldSchema, Line, Page
from docauto.provider import ExtractRequest, ProviderContext, ProviderInfo, Capability

mod = load_option("tier3_ai/llm_openai_compatible/provider.py")


def doc():
    p = Page(pageNumber=1, width=1, height=1, lines=[
        Line(text="Invoice Number: INV-7", bbox=BBox(x=0.1, y=0.1, w=0.4, h=0.02), confidence=0.96),
        Line(text="الإجمالي: 1,150.00", bbox=BBox(x=0.5, y=0.3, w=0.4, h=0.02), confidence=0.9)])
    d = DocumentResult(documentId="d", pages=[p]); d.rebuild_text()
    return d


SCHEMA = FieldSchema(name="Invoice", fields=[FieldDef(name="invoice_number"),
                                             FieldDef(name="total", type="currency"),
                                             FieldDef(name="supplier")])


def provider(monkeypatch, reply, status=200):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        content = reply if isinstance(reply, str) else globals()["json"].dumps(reply)
        return httpx.Response(status, json={"choices": [{"message": {"content": content}}]},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(mod.httpx, "post", fake_post)
    p = mod.LlmExtractionProvider()
    p.info = ProviderInfo(id="llm-cloud", displayName="", version="1", vendor="", tier=3, kind="extraction",
                          capabilities=[Capability.EXTRACT_SCHEMA], languages=["*"], runsLocally=False)
    p.init(ProviderContext(config={"baseUrl": "https://llm.example/v1", "model": "m"}, credentials={"apiKey": "k"}))
    return p, calls


def test_prompt_contains_line_ids():
    prompt, index = mod.build_prompt(doc(), SCHEMA)
    assert "[p1.l0] Invoice Number: INV-7" in prompt and "p1.l1" in index


def test_grounded_fields_and_hallucination_guard(monkeypatch):
    reply = {"fields": {"invoice_number": {"value": "INV-7", "lines": ["p1.l0"], "confidence": 0.95},
                        "total": {"value": "1150.00", "lines": ["p1.l1"], "confidence": 0.9},
                        "supplier": {"value": "Imaginary Corp", "lines": [], "confidence": 0.9}}}
    p, calls = provider(monkeypatch, reply)
    out = {f.name: f for f in p.extract(ExtractRequest(document=doc(), schema=SCHEMA))}
    assert calls[0]["headers"]["Authorization"] == "Bearer k"
    assert calls[0]["json"]["response_format"] == {"type": "json_object"}
    assert out["invoice_number"].value == "INV-7" and out["invoice_number"].bbox.x == pytest.approx(0.1)
    assert out["total"].value == 1150.0 and out["total"].page == 1
    assert out["supplier"].confidence < 0.5
    assert "hallucination" in out["supplier"].validation.errors[0]


def test_invalid_json_is_repaired(monkeypatch):
    replies = iter(["sorry, here you go", json.dumps({"fields": {"invoice_number": {"value": "INV-7",
                                                                                     "lines": ["p1.l0"]}}})])

    def fake_post(url, json=None, headers=None, timeout=None):
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]},
                              request=httpx.Request("POST", url))

    p, _ = provider(monkeypatch, "{}")
    monkeypatch.setattr(mod.httpx, "post", fake_post)
    out = p.extract(ExtractRequest(document=doc(), schema=SCHEMA))
    assert out[0].value == "INV-7"


def test_auth_error_mapped(monkeypatch):
    p, _ = provider(monkeypatch, "{}", status=401)
    with pytest.raises(AuthFailed):
        p.extract(ExtractRequest(document=doc(), schema=SCHEMA))
