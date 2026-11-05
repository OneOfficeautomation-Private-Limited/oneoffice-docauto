"""Test sets (quality gate): ground truth from reviewed documents or uploads, engine combinations evaluated and kept."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, SAMPLES, make_config

DF = "/api/docs/v1/df"
PDF = (SAMPLES / "invoice_digital.pdf").read_bytes()
TRUTH = {"invoice_number": "INV-2026-0042", "invoice_date": "2026-10-15", "supplier_vat": "300123456700003",
         "subtotal": 10000.0, "vat_amount": 1500.0, "total": 11500.0}


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


def reviewed_document(client):
    """An invoice processed in a strict process, sent to review and approved by a reviewer."""
    fid = client.post(f"{DF}/flows", json={"name": "Strict", "docTypes": ["invoice"],
                                           "review": {"fieldThresholds": {"total": 0.999}}}).json()["id"]
    r = client.post(f"{DF}/flows/{fid}/documents", params={"wait": "true"},
                    files=[("files", ("inv.pdf", PDF, "application/pdf"))]).json()["documents"][0]
    assert r["status"] == "validation"
    client.post(f"{DF}/documents/{r['documentId']}/review", json={"reviewer": "qa", "fields": {"total": 11500.0}})
    return r["documentId"]


def test_build_a_set_and_compare_engines(client):
    s = client.post(f"{DF}/testsets", json={"name": "AP invoices", "docType": "invoice"}).json()
    assert s["id"] == "ap-invoices" and s["itemCount"] == 0 and s["lastRun"] is None
    assert client.post(f"{DF}/testsets", json={"name": "x", "docType": "nope"}).status_code == 400

    up = client.post(f"{DF}/testsets/{s['id']}/items", data={"expected": json.dumps(TRUTH), "tags": "pdf, digital"},
                     files={"file": ("inv.pdf", PDF, "application/pdf")})
    assert up.status_code == 200, up.text
    item = up.json()
    assert item["expected"]["total"] == 11500.0 and item["tags"] == ["pdf", "digital"] and item["source"] == "upload"
    bad = client.post(f"{DF}/testsets/{s['id']}/items", data={"expected": json.dumps({"colour": "red"})},
                      files={"file": ("inv.pdf", PDF, "application/pdf")})
    assert bad.status_code == 400

    doc = reviewed_document(client)
    added = client.post(f"{DF}/testsets/{s['id']}/documents", json={"documentId": doc}).json()
    assert added["source"] == "document" and added["sourceDocumentId"] == doc
    assert added["expected"]["customer_name"] == "Oneoffice Automation" and added["expected"]["total"] == 11500.0
    assert client.get(f"{DF}/testsets/{s['id']}/items/{added['id']}/file").content == PDF

    before = documents(client)
    run = client.post(f"{DF}/testsets/{s['id']}/runs", params={"wait": "true"}, json={"combos": [
        {"ocr": "auto", "extraction": "builtin-rules"}, {"ocr": "pdf-text", "extraction": "builtin-rules"},
        {"ocr": "no-such-engine", "extraction": "auto"}]})
    assert run.status_code == 202, run.text
    run = run.json()
    assert run["status"] == "completed" and len(run["rows"]) == 4
    auto, pdf_text, missing = run["results"]
    assert missing["skipped"] and "unknown engine" in missing["skipped"]
    for r in (auto, pdf_text):
        assert r["documents"] == 2 and r["errors"] == 0 and r["fieldAccuracy"] == 1.0
        assert r["byField"]["total"] == 1.0 and r["wronglyAutoApproved"] == 0
        assert r["costUsd"] == 0.0 and r["costKnown"] and r["secondsPerPage"] > 0
    assert auto["label"] == "auto × builtin-rules"
    row = run["rows"][0]
    assert row["fields"]["total"] == {"want": 11500.0, "got": 11500.0, "ok": True} and row["itemId"]
    assert documents(client) == before                                    # nothing kept

    listed = client.get(f"{DF}/testsets", params={"docType": "invoice"}).json()
    assert listed[0]["itemCount"] == 2 and listed[0]["lastRun"]["id"] == run["id"]
    assert "rows" not in listed[0]["lastRun"]
    runs = client.get(f"{DF}/testsets/{s['id']}/runs").json()
    assert [r["id"] for r in runs] == [run["id"]] and "rows" not in runs[0]
    assert client.get(f"{DF}/testsets/{s['id']}/runs/{run['id']}").json()["rows"][0]["itemId"]

    upd = client.put(f"{DF}/testsets/{s['id']}/items/{item['id']}", json={"expected": {"total": 1.0}}).json()
    assert upd["expected"] == {"total": 1.0}
    assert client.delete(f"{DF}/testsets/{s['id']}/items/{item['id']}").status_code == 200
    assert client.get(f"{DF}/testsets/{s['id']}").json()["itemCount"] == 1
    assert client.delete(f"{DF}/testsets/{s['id']}").status_code == 200
    assert client.get(f"{DF}/testsets/{s['id']}").status_code == 400


def test_only_reviewed_documents_become_ground_truth(client):
    s = client.post(f"{DF}/testsets", json={"name": "AP", "docType": "invoice"}).json()
    fid = client.post(f"{DF}/flows", json={"name": "AP", "docTypes": ["invoice"]}).json()["id"]
    d = client.post(f"{DF}/flows/{fid}/documents", params={"wait": "true"},
                    files=[("files", ("inv.pdf", PDF, "application/pdf"))]).json()["documents"][0]
    r = client.post(f"{DF}/testsets/{s['id']}/documents", json={"documentId": d["documentId"]})
    assert r.status_code == 400 and "reviewed" in r.json()["message"]
    assert client.post(f"{DF}/testsets/{s['id']}/runs", json={}).status_code == 400          # empty set


def test_background_run_and_seed_set(client):
    import time
    seeded = client.post(f"{DF}/testsets/seed", params={"count": 1}).json()
    tags = sorted(t for i in seeded["items"] for t in i["tags"])
    assert seeded["docType"] == "invoice" and tags == ["bilingual", "clean", "fax", "pdf", "scan"]
    again = client.post(f"{DF}/testsets/seed", params={"count": 1}).json()              # topped up, not duplicated
    assert again["id"] == seeded["id"] and again["itemCount"] == 5
    combos = [{"ocr": "pdf-text", "extraction": "builtin-rules"}]
    if shutil.which("tesseract"):
        combos.append({"ocr": "tesseract", "extraction": "builtin-rules"})
    run = client.post(f"{DF}/testsets/{seeded['id']}/runs", json={"combos": combos}).json()
    assert run["status"] == "running"
    deadline = time.time() + 240
    while time.time() < deadline:
        run = client.get(f"{DF}/testsets/{seeded['id']}/runs/{run['id']}").json()
        if run["status"] != "running":
            break
        time.sleep(0.5)
    assert run["status"] == "completed", run
    pdf_only = run["results"][0]
    assert pdf_only["documents"] == 5 and pdf_only["errors"] == 4                       # pdf-text reads PDFs only
    if len(run["results"]) > 1:
        tess = run["results"][1]
        assert tess["errors"] == 0 and tess["fieldAccuracy"] > 0.6
        bilingual = next(r for r in run["rows"] if r["ocr"] == "tesseract" and "bilingual" in r["tags"])
        assert bilingual["fields"]["total"]["ok"]
