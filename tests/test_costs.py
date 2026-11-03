"""Cost model: list prices per 1,000 pages and the monthly estimate (the strategy plan's $33 example)."""
from fastapi.testclient import TestClient

from conftest import make_config
from docauto import costs


def test_plan_example_is_33_dollars():
    e = costs.estimate(10000, 0.2, 0.3, "azure-di", "azure-di")
    assert [l["usd"] for l in e["lines"]] == [0.0, 3.0, 30.0] and e["total"] == 33.0 and e["complete"]
    assert e["lines"][0]["pages"] == 8000


def test_claude_prices_follow_the_model():
    assert costs.claude_per_1000("claude-opus-5-5") == 32.0
    assert costs.claude_per_1000("claude-sonnet-5-5") == 16.0       # the plan's $10-20 mid-tier range
    assert costs.claude_per_1000("claude-haiku-5-5") == 0.8
    e = costs.estimate(10000, 0.2, 0.3, "azure-di", "claude", "claude-sonnet-5-5")
    assert e["total"] == 3.0 + 48.0


def test_local_only_costs_nothing():
    e = costs.estimate(10000, 0.2, 0.3, "azure-di", "claude", cloud_opt_in=False)
    assert e["total"] == 0.0 and e["lines"][1]["pages"] == 0 and "opt-in" in e["note"]
    assert costs.estimate(10000, 0.2, 0.3, None, "builtin-rules")["total"] == 0.0


def test_unknown_price_is_flagged_not_zero():
    e = costs.estimate(1000, 0.5, 0.5, "abbyy", "builtin-rules")
    assert e["lines"][1]["usd"] is None and not e["complete"]


def test_costs_endpoint():
    from docauto.api.app import app, set_service
    from docauto.pipeline import DocumentService
    set_service(DocumentService(make_config()))
    body = TestClient(app).get("/api/docs/v1/costs").json()
    assert body["cloudOptIn"] is False and body["estimate"]["total"] == 0.0      # shipped config: local-only
    assert body["prices"]["tesseract"]["ocr"] == 0.0 and body["prices"]["azure-di"]["ocr"] == 1.5
    assert body["prices"]["claude"]["extract"] == 32.0 and body["claudeModel"] == "claude-opus-5-5"
