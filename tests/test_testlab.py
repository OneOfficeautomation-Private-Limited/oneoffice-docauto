"""Test lab: a document read as a document type by one or two engines, side by side, and nothing kept."""
import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, SAMPLES, make_config

DF = "/api/docs/v1/df"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DOCAUTO_FOLDER_WATCHER", "false")
    monkeypatch.setenv("DOCAUTO_CONFIG", str(ROOT / "config" / "document-service.yaml"))
    monkeypatch.delenv("DOCAUTO_API_TOKEN", raising=False)
    import docauto.api.app as appmod
    from docauto.pipeline import DocumentService
    appmod.set_service(DocumentService(make_config()))
    return TestClient(appmod.app)


def documents(client):
    import docauto.api.app as appmod
    return appmod.service().store._one("SELECT COUNT(*) n FROM documents")["n"]


def test_reads_a_document_as_a_type_and_keeps_nothing(client):
    before = documents(client)
    r = client.post(f"{DF}/test", data={"docType": "invoice", "engines": "auto,pdf-text"},
                    files={"file": ("invoice_digital.pdf", (SAMPLES / "invoice_digital.pdf").read_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [f["name"] for f in body["fields"]][:2] == ["invoice_number", "invoice_date"]
    assert len(body["runs"]) == 2
    for run in body["runs"]:
        assert "error" not in run, run
        assert run["fields"]["invoice_number"]["value"] == "INV-2026-0042"
        assert run["readBy"] == ["pdf-text"] and run["ms"] >= 0
    assert documents(client) == before


def test_an_engine_that_cannot_run_says_why(client):
    r = client.post(f"{DF}/test", data={"docType": "invoice", "engines": "no-such-engine"},
                    files={"file": ("invoice_digital.pdf", (SAMPLES / "invoice_digital.pdf").read_bytes(), "application/pdf")})
    assert r.status_code == 200
    assert r.json()["runs"][0]["error"]


def test_unknown_document_type(client):
    r = client.post(f"{DF}/test", data={"docType": "nope"}, files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 400
