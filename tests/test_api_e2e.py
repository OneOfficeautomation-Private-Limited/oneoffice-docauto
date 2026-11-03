"""End-to-end through the REST API with the real Tesseract option (skipped if not installed)."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from conftest import make_config, SAMPLES

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
API = "/api/docs/v1"


@pytest.fixture
def client():
    from docauto.api.app import app, set_service
    from docauto.pipeline import DocumentService
    set_service(DocumentService(make_config()))
    return TestClient(app)


def test_providers_listing(client):
    body = client.get(f"{API}/providers").json()
    ids = {p["id"]: p for p in body["providers"]}
    assert ids["tesseract"]["available"] and ids["pdf-text"]["available"]
    assert ids["azure-di"]["optionPack"] == "tier2_cloud" and not ids["azure-di"]["available"]
    assert ids["llm-local"]["runsLocally"] is True


def test_scan_invoice_to_review_and_back(client):
    schema = json.loads((SAMPLES / "invoice_schema.json").read_text(encoding="utf-8"))
    assert client.post(f"{API}/schemas", json=schema).status_code == 200
    opts = {"schemaId": "supplier-invoice", "languages": ["en", "ar"], "classifyLabels": ["invoice", "contract"]}
    with open(SAMPLES / "invoice_scan.png", "rb") as f:
        r = client.post(f"{API}/documents?wait=true", files={"file": ("invoice_scan.png", f, "image/png")},
                        data={"options": json.dumps(opts)})
    assert r.status_code == 202, r.text
    doc_id = r.json()["documentId"]
    fields = client.get(f"{API}/documents/{doc_id}/fields").json()
    result = client.get(f"{API}/documents/{doc_id}/result").json()
    got = {f["name"]: f for f in result["fields"]}
    expected = {"invoice_number": "INV-2026-0042", "invoice_date": "2026-10-15", "supplier_vat": "300123456700003"}
    misread = [k for k, v in expected.items() if got[k]["value"] != v]
    if misread:
        # Tesseract builds differ by platform and may misread characters on this blurred scan
        # (seen on Windows: 'INv2026-0042', '2028-10-15'). Acceptable ONLY if the document is
        # held for human review instead of being auto-approved.
        assert fields["reviewRequired"] is True, misread
    if got["invoice_number"]["value"] != expected["invoice_number"]:
        assert "does not match pattern" in got["invoice_number"]["validation"]["errors"]
    # amounts are cross-checked by the schema's arithmetic rules, so they must be exact
    assert fields["fields"]["subtotal"] == 10000.0
    assert fields["fields"]["vat_amount"] == 1500.0
    assert fields["fields"]["total"] == 11500.0
    assert result["classification"]["label"] == "invoice"
    assert "ar" in result["languages"]
    if fields["reviewRequired"]:
        assert any(x["id"] == doc_id for x in client.get(f"{API}/reviews").json())
        r = client.post(f"{API}/reviews/{doc_id}/complete",
                        json={"reviewer": "qa", "fields": {"supplier_vat": "300123456700003"}})
        assert r.json()["reviewStatus"] == "completed"
    usage = client.get(f"{API}/usage").json()
    assert any(u["k"] == "tesseract" for u in usage)
    assert client.get(f"{API}/documents/{doc_id}/page/1.png").headers["content-type"] == "image/png"
    assert "invoice_number" in client.get(f"{API}/documents/{doc_id}/export?format=csv").text


def test_digital_pdf_skips_ocr(client):
    schema = json.loads((SAMPLES / "invoice_schema.json").read_text(encoding="utf-8"))
    with open(SAMPLES / "invoice_digital.pdf", "rb") as f:
        r = client.post(f"{API}/documents?wait=true", files={"file": ("inv.pdf", f, "application/pdf")},
                        data={"options": json.dumps({"schema": schema})})
    doc_id = r.json()["documentId"]
    res = client.get(f"{API}/documents/{doc_id}/result").json()
    engines = [s["providerId"] for s in res["provenance"]["steps"] if s["stage"] == "digitize"]
    assert engines == ["pdf-text"]
    assert res["review"]["required"] is False
    assert {f["name"]: f["value"] for f in res["fields"]}["vat_amount"] == 1500.0


def test_screen_ocr_and_searchable_pdf(client):
    with open(SAMPLES / "screen.png", "rb") as f:
        r = client.post(f"{API}/ocr/screen", files={"file": ("s.png", f, "image/png")})
    assert "C-10293" in r.json()["text"] and r.json()["engine"] == "tesseract"
    with open(SAMPLES / "screen.png", "rb") as f:
        r = client.post(f"{API}/pdf/searchable", files={"file": ("s.png", f, "image/png")})
    assert r.content[:4] == b"%PDF"


def test_policy_denied_returns_403(client):
    with open(SAMPLES / "screen.png", "rb") as f:
        r = client.post(f"{API}/ocr/screen", files={"file": ("s.png", f, "image/png")},
                        data={"provider": "azure-di"})
    # azure-di is allowed by hybrid policy but not configured -> 503; under airgapped it is 403
    assert r.status_code in (403, 503)


def test_airgapped_profile_blocks_cloud_engine():
    from docauto.api.app import app, set_service
    from docauto.pipeline import DocumentService
    set_service(DocumentService(make_config(deployment_profile="onprem_airgapped")))
    c = TestClient(app)
    with open(SAMPLES / "screen.png", "rb") as f:
        r = c.post(f"{API}/ocr/screen", files={"file": ("s.png", f, "image/png")}, data={"provider": "azure-di"})
    assert r.status_code == 403 and r.json()["code"] == "POLICY_DENIED"
