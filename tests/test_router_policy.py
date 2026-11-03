"""Deployment profiles, allow/deny, residency, routing order and fallback."""
import pytest

from conftest import make_config
from docauto.errors import PolicyDenied, ProviderError, UnsupportedCapability
from docauto.models import DocumentResult, Line, Page, BBox
from docauto.policy import PolicyEngine
from docauto.provider import Capability, OcrProvider, ProviderInfo
from docauto.registry import ProviderRegistry
from docauto.router import Router


def info(pid, local=True, regions=(), cost=0, tier=1, caps=(Capability.OCR_PRINTED,)):
    return ProviderInfo(id=pid, displayName=pid, version="1", vendor="t", tier=tier, kind="ocr",
                        capabilities=list(caps), languages=["*"], runsLocally=local, regions=list(regions),
                        creditCostPerPage={c.value: cost for c in caps})


class Fake(OcrProvider):
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def recognize(self, req):
        self.calls += 1
        if self.fail:
            raise ProviderError("boom", self.info.id)
        return DocumentResult(documentId="d", pages=[Page(pageNumber=1, width=1, height=1, lines=[
            Line(text=self.info.id, bbox=BBox(x=0, y=0, w=1, h=1), confidence=0.9)])])


def test_airgapped_blocks_cloud():
    pol = PolicyEngine({"deployment_profile": "onprem_airgapped"})
    assert pol.allows(info("tesseract"))
    ok, why = pol.check(info("azure-di", local=False, regions=["westeurope"]))
    assert not ok and "airgapped" in why


def test_residency_and_deny():
    pol = PolicyEngine({"deployment_profile": "onprem_hybrid", "residency": {"allowed_regions": ["uaenorth"]},
                        "providers": {"allow": ["*"], "deny": ["aws-*"]}})
    assert pol.allows(info("azure-di", local=False, regions=["uaenorth"]))
    assert not pol.allows(info("google-docai", local=False, regions=["us"]))
    assert not pol.allows(info("aws-textract", local=False, regions=["uaenorth"]))
    assert not pol.allows(info("llm-cloud", local=False, regions=[]))  # unknown region -> denied


def test_unknown_profile_rejected():
    with pytest.raises(ValueError):
        PolicyEngine({"deployment_profile": "everywhere"})


def _router(cfg, providers):
    reg = ProviderRegistry(cfg)
    for inst, inf in providers:
        reg.register(inst, inf)
    return Router(reg, PolicyEngine(cfg), cfg), reg


CLOUD_OK = {"cloud_opt_in": True}


def test_cloud_opt_in_gates_outbound_engines():
    cloud = info("azure-di", local=False, regions=["westeurope"])
    assert PolicyEngine({"deployment_profile": "cloud_saas"}).allows(cloud)          # legacy configs: allowed
    off = PolicyEngine({"deployment_profile": "cloud_saas", "routing": {"cloud_opt_in": False}})
    ok, why = off.check(cloud)
    assert not ok and "opt-in" in why and off.allows(info("tesseract"))
    assert not PolicyEngine(make_config()).allows(cloud)                               # shipped config: local-only
    assert PolicyEngine({"deployment_profile": "onprem_hybrid", "routing": {"cloud_opt_in": "true"}}).allows(cloud)


def test_default_then_cheapest_and_fallback_order():
    cfg = make_config(deployment_profile="cloud_saas", routing=CLOUD_OK,
                      defaults={"document_ocr": "premium"}, fallback={"on_error": ["local"]})
    cfg["router"] = {"max_retries": 0}
    r, _ = _router(cfg, [(Fake(), info("local", cost=0)), (Fake(), info("cheap", local=False, cost=1)),
                         (Fake(), info("premium", local=False, cost=5))])
    ids = [c.info.id for c in r.candidates("ocr", [Capability.OCR_PRINTED], ["en"], None, "document_ocr")]
    assert ids == ["premium", "cheap", "local"]


def test_explicit_provider_denied_by_policy():
    cfg = make_config(deployment_profile="onprem_airgapped")
    r, _ = _router(cfg, [(Fake(), info("azure-di", local=False))])
    with pytest.raises(PolicyDenied):
        r.candidates("ocr", [Capability.OCR_PRINTED], [], "azure-di")


def test_missing_capability():
    cfg = make_config(deployment_profile="cloud_saas")
    r, _ = _router(cfg, [(Fake(), info("t"))])
    with pytest.raises(UnsupportedCapability):
        r.candidates("ocr", [Capability.TABLES], [])


def test_fallback_on_error_records_provenance():
    cfg = make_config(deployment_profile="cloud_saas", routing=CLOUD_OK, fallback={"on_error": ["local"]})
    cfg["router"] = {"max_retries": 1, "backoff_seconds": 0}
    bad, good = Fake(fail=True), Fake()
    r, _ = _router(cfg, [(bad, info("cloud", local=False, cost=0)), (good, info("local", cost=0))])
    cands = r.candidates("ocr", [Capability.OCR_PRINTED], [])
    assert [c.info.id for c in cands] == ["cloud", "local"]
    result, att = r.run(cands, lambda p: p.instance.recognize(None))
    assert att.provider_id == "local" and att.fallback_from == "cloud"
    assert bad.calls == 2  # retried once (retryable error), then fell back


def test_default_engine_stays_first_even_if_listed_as_fallback():
    cfg = make_config(deployment_profile="cloud_saas", defaults={"document_ocr": "local"},
                      fallback={"on_error": ["local"]})
    r, _ = _router(cfg, [(Fake(), info("local", cost=0)), (Fake(), info("cloud", local=False, cost=0))])
    ids = [c.info.id for c in r.candidates("ocr", [Capability.OCR_PRINTED], [], None, "document_ocr")]
    assert ids[0] == "local"


class Paged(OcrProvider):
    """One line per page at a fixed confidence; counts pages it was asked to read."""
    def __init__(self, conf):
        self.conf, self.pages_read = conf, 0

    def recognize(self, req):
        self.pages_read += len(req.pages)
        return DocumentResult(documentId="d", pages=[Page(pageNumber=n, width=1, height=1, lines=[
            Line(text=f"{self.info.id} page", bbox=BBox(x=0, y=0, w=1, h=1), confidence=self.conf)])
            for n, _ in enumerate(req.pages, 1)])


def _retry_service(opt_in, local_conf=0.95):
    from PIL import Image
    from docauto.pipeline import DocumentService, ProcessOptions
    cfg = make_config(deployment_profile="onprem_hybrid", routing={"cloud_opt_in": opt_in},
                      defaults={"document_ocr": "main", "extraction": "builtin-rules"},
                      fallback={"on_error": [], "low_confidence": {"threshold": 0.85, "retry_local": "local2",
                                                                    "escalate_to": "cloud"}})
    svc = DocumentService(cfg)
    engines = {"main": Paged(0.60), "local2": Paged(local_conf), "cloud": Paged(0.99)}
    for pid, e in engines.items():
        svc.registry.register(e, info(pid, local=pid != "cloud", cost=0 if pid != "cloud" else 1))
    import io
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, format="PNG")
    return svc, engines, buf.getvalue(), ProcessOptions()


def test_weak_pages_retried_locally_first():
    svc, eng, png, opt = _retry_service(opt_in=True)
    r = svc.process_bytes(png, "p.png", opt)
    assert "local2 page" in r.text and eng["cloud"].pages_read == 0       # local fixed it: no cloud cost
    assert [s.providerId for s in r.provenance.steps if s.stage == "digitize"] == ["main", "local2"]


def test_cloud_retry_needs_opt_in():
    svc, eng, png, opt = _retry_service(opt_in=False, local_conf=0.70)
    r = svc.process_bytes(png, "p.png", opt)
    assert eng["cloud"].pages_read == 0 and "local2 page" in r.text       # local-only: best local read kept
    chain = {s["id"]: s for s in svc.retry_chain("main")}
    assert chain["local2"]["runs"] and not chain["cloud"]["runs"] and "opt-in" in chain["cloud"]["reason"]
    svc, eng, png, opt = _retry_service(opt_in=True, local_conf=0.70)
    r = svc.process_bytes(png, "p.png", opt)
    assert eng["cloud"].pages_read == 1 and "cloud page" in r.text
    assert any(s.providerId == "cloud" and s.fallbackFrom == "main" and s.credits == 1 for s in r.provenance.steps)


def test_strict_provider_has_no_fallback():
    cfg = make_config(deployment_profile="cloud_saas")
    r, _ = _router(cfg, [(Fake(), info("a")), (Fake(), info("b"))])
    assert [c.info.id for c in r.candidates("ocr", [Capability.OCR_PRINTED], [], "b", strict=True)] == ["b"]
