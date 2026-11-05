"""Escalation: an AI extraction below the review threshold is re-read with the engine's escalation model."""
import pytest

from conftest import SAMPLES, make_config
from docauto.models import ExtractedField, FieldDef, FieldSchema
from docauto.pipeline import DocumentService, ProcessOptions
from docauto.provider import Capability, ExtractionProvider, ProviderInfo

SCHEMA = FieldSchema(name="Invoice", fields=[FieldDef(name="invoice_number", required=True),
                                             FieldDef(name="total", type="currency", required=True)])


class FakeAI(ExtractionProvider):
    """Stands in for Claude: one confidence per model, records which models were asked."""

    def __init__(self, conf: dict):
        self.conf, self.calls = conf, []

    def init(self, ctx):
        super().init(ctx)
        self.model, self.escalation_model = "small", ctx.config.get("escalationModel", "big")

    def extract(self, req):
        m = req.model or self.model
        self.calls.append(m)
        c = self.conf[m]
        return [ExtractedField(name="invoice_number", value="INV-2026-0042", confidence=c, providerId="fake-ai"),
                ExtractedField(name="total", type="currency", value=11500.0, confidence=c, providerId="fake-ai")]


def run(conf, **cfg):
    svc = DocumentService(make_config())
    fake = FakeAI(conf)
    svc.registry.register(fake, ProviderInfo(
        id="fake-ai", displayName="", version="1", vendor="", tier=3, kind="extraction",
        capabilities=[Capability.EXTRACT_SCHEMA], languages=["*"], runsLocally=True,
        creditCostPerPage={"extract_schema": 20}, optionPack="tier3_ai"), {"config": cfg})
    res = svc.process_bytes((SAMPLES / "invoice_digital.pdf").read_bytes(), "inv.pdf",
                            ProcessOptions(schema=SCHEMA, extractionProvider="fake-ai"))
    usage = [dict(r) for r in svc.store._all("SELECT provider_id, model FROM usage WHERE provider_id='fake-ai'")]
    return fake, res, usage


def test_low_confidence_is_re_read_and_the_better_result_kept():
    fake, res, usage = run({"small": 0.5, "big": 0.95})
    assert fake.calls == ["small", "big"]
    steps = [s for s in res.provenance.steps if s.stage == "extract"]
    assert [(s.model, s.selected) for s in steps] == [("small", False), ("big", True)]
    assert [u["model"] for u in usage] == ["small", "big"]
    assert res.fields[0].confidence >= 0.95 and not res.review.required
    assert any("escalated from small to big: kept the big result" in w for w in res.warnings)


def test_a_worse_second_reading_is_dropped():
    fake, res, _ = run({"small": 0.5, "big": 0.3})
    steps = [s for s in res.provenance.steps if s.stage == "extract"]
    assert [s.selected for s in steps] == [True, False] and res.fields[0].confidence == pytest.approx(0.5, abs=0.2)
    assert res.review.required


def test_no_escalation_when_confident_or_switched_off():
    fake, res, _ = run({"small": 0.95, "big": 0.99})
    assert fake.calls == ["small"] and [s.selected for s in res.provenance.steps if s.stage == "extract"] == [None]
    fake, _, _ = run({"small": 0.5, "big": 0.99}, escalationModel=None)
    assert fake.calls == ["small"]
