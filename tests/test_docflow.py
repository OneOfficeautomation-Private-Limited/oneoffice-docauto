"""Oneoffice DocFlow: doc types, flows, smart inbox (classify & split), rules, lookups, deliveries, teach, SLA."""
import io
import json
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLES, make_config

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
DF = "/api/docs/v1/df"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DOCAUTO_FOLDER_WATCHER", "false")
    import docauto.api.app as appmod
    from docauto.pipeline import DocumentService
    appmod.set_service(DocumentService(make_config()))
    appmod._df = None
    return TestClient(appmod.app)


def pdf(pages):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for lines in pages:
        y = 800
        for t in lines:
            c.setFont("Helvetica", 12)
            c.drawString(60, y, t)
            y -= 22
        c.showPage()
    c.save()
    return buf.getvalue()


INV = ["ACME Trading Co. LLC", "Tax Invoice", "Invoice Number: INV-2026-0042", "Invoice Date: 15/10/2026",
       "Supplier VAT No: 300123456700003", "Customer: Oneoffice Automation", "Subtotal: 10,000.00",
       "VAT 15%: 1,500.00", "Total: 11,500.00 SAR"]
PO = ["Oneoffice Automation", "Purchase Order", "PO Number: PO-7781", "PO Date: 01/10/2026", "Vendor: ACME Trading",
      "Total: 8,000.00"]


def up(c, flow, data, name="doc.pdf", **q):
    r = c.post(f"{DF}/flows/{flow}/documents", params={"wait": "true", **q}, files=[("files", (name, data, "application/pdf"))])
    assert r.status_code == 202, r.text
    return r.json()["documents"][0]


def test_seeded_library_and_inbox(client):
    types = {t["id"] for t in client.get(f"{DF}/doctypes").json()}
    assert {"invoice", "purchase_order", "receipt", "bank_statement", "id_document"} <= types
    flows = client.get(f"{DF}/flows").json()
    assert flows[0]["id"] == "smart-inbox" and flows[0]["isInbox"] and flows[0]["status"] == "live"


def test_smart_inbox_splits_mixed_batch(client):
    batch = pdf([INV, PO, INV[:3] + ["Invoice Date: 20/10/2026", "Subtotal: 100.00", "VAT 15%: 15.00", "Total: 115.00"]])
    d = up(client, "smart-inbox", batch, "scan_batch.pdf")
    parent = client.get(f"{DF}/documents/{d['documentId']}").json()
    assert parent["status"] == "split" and len(parent["children"]) == 3
    kinds = [ch["doctype_id"] for ch in parent["children"]]
    assert kinds == ["invoice", "purchase_order", "invoice"]
    po = client.get(f"{DF}/documents/{parent['children'][1]['id']}").json()
    assert po["fields"]["po_number"] == "PO-7781" and po["parent_id"] == d["documentId"]
    # child file only contains its own page
    f = client.get(f"/api/docs/v1/documents/{po['id']}/file").content
    import pypdfium2 as pdfium
    assert len(pdfium.PdfDocument(f)) == 1


def test_unclassified_goes_to_review_and_reclassify(client):
    d = up(client, "smart-inbox", pdf([["Hello world", "nothing to see here"]]), "random.pdf")
    child = client.get(f"{DF}/documents/{d['documentId']}").json()["children"][0]
    assert child["status"] == "validation" and child["doctype_id"] is None
    q = client.get(f"{DF}/review").json()
    assert any(x["id"] == child["id"] and x["doctypeName"] == "Unclassified" for x in q)
    r = client.post(f"{DF}/documents/{child['id']}/reclassify", json={"docType": "receipt"}).json()
    assert r["doctype_id"] == "receipt"


def test_flow_rules_lookups_and_deliveries(client, tmp_path, monkeypatch):
    # reference data: supplier master
    csv = "vat,name,approved\n300123456700003,ACME Trading Co. LLC,yes\n".encode()
    t = client.post(f"{DF}/reftables", files={"file": ("suppliers.csv", csv, "text/csv")}, data={"name": "Suppliers"}).json()
    assert t["rowCount"] == 1 and t["columns"] == ["vat", "name", "approved"]
    posted = []
    import docauto.docflow as dfm
    monkeypatch.setattr(dfm.httpx, "post", lambda url, json=None, headers=None, timeout=None:
                        posted.append((url, json)) or type("R", (), {"status_code": 200})())
    flow = {"name": "AP invoices", "docTypes": ["invoice"],
            "rules": [{"type": "duplicate", "name": "Duplicate invoice", "params": {"fields": ["supplier_vat", "invoice_number"]}},
                      {"type": "lookup", "name": "Known supplier", "params": {"field": "supplier_vat", "table": "suppliers",
                                                                              "column": "vat", "enrich": {"supplier_name": "name"}}},
                      {"type": "ksa_vat", "name": "ZATCA VAT"},
                      {"type": "threshold", "name": "Approval above 50k", "params": {"field": "total", "op": ">", "value": 50000}}],
            "deliveries": [{"type": "webhook", "name": "ERP", "params": {"url": "http://erp.local/hook"}},
                           {"type": "excel", "name": "AP register", "params": {"path": str(tmp_path / "ap.xlsx")}},
                           {"type": "folder", "name": "Archive", "params": {"path": str(tmp_path / "out")},
                            "on": ["success", "validation", "validated"]},
                           {"type": "start_bot", "name": "Post to ERP bot", "params": {"url": "http://rpa.local/run", "bot": "PostInvoice"}}]}
    r = client.post(f"{DF}/flows", json=flow)
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    d1 = up(client, fid, pdf([INV]))
    doc1 = client.get(f"{DF}/documents/{d1['documentId']}").json()
    assert doc1["status"] == "success", doc1["flags"]
    assert doc1["fields"]["supplier_name"] == "ACME Trading Co. LLC"        # enriched from reference data
    deadline = time.time() + 10
    while time.time() < deadline and len(posted) < 2:
        time.sleep(0.2)
    urls = {u for u, _ in posted}
    assert {"http://erp.local/hook", "http://rpa.local/run"} <= urls
    bot = next(j for u, j in posted if u == "http://rpa.local/run")
    assert bot["bot"] == "PostInvoice" and bot["input"]["fields"]["total"] == 11500.0
    while time.time() < deadline and not (tmp_path / "ap.xlsx").exists():
        time.sleep(0.2)
    from openpyxl import load_workbook
    ws = load_workbook(tmp_path / "ap.xlsx")["Invoice"]
    assert ws.max_row == 2 and "invoice_number" in [c.value for c in ws[1]]
    # same invoice again -> duplicate flag -> review desk
    d2 = up(client, fid, pdf([INV]))
    doc2 = client.get(f"{DF}/documents/{d2['documentId']}").json()
    assert doc2["status"] == "validation" and any(f["type"] == "duplicate" for f in doc2["flags"])
    # unknown supplier + big amount + wrong VAT
    bad = [l.replace("300123456700003", "123456789012345").replace("11,500.00", "99,000.00") for l in INV]
    bad = [l if not l.startswith("Invoice Number") else "Invoice Number: INV-2026-0099" for l in bad]
    d3 = up(client, fid, pdf([bad]))
    flags = {f["type"] for f in client.get(f"{DF}/documents/{d3['documentId']}").json()["flags"]}
    assert {"lookup", "ksa_vat", "threshold"} <= flags
    log = client.get(f"{DF}/flows/{fid}/deliveries").json()
    assert any(x["delivery"] == "ERP" and x["ok"] for x in log)
    # review desk + export
    q = client.get(f"{DF}/review", params={"flow": fid}).json()
    assert len(q) == 2 and all("slaHours" in x for x in q)
    r = client.post(f"{DF}/documents/{d2['documentId']}/review", json={"reviewer": "qa", "action": "invalid"}).json()
    assert r["status"] == "invalid"
    assert "INV-2026-0042" in client.get(f"{DF}/flows/{fid}/export").text


def test_teach_from_samples(client):
    s = client.post(f"{DF}/doctypes/suggest", files=[("files", ("a.pdf", pdf([PO]), "application/pdf"))]).json()
    assert s["category"] == "purchase_order"
    names = {f["name"] for f in s["fields"]}
    assert {"po_number", "po_date", "total"} <= names
    vendor = next(f for f in s["fields"] if f["name"] == "vendor_name")
    assert vendor["samples"] == ["ACME Trading"]
    custom = client.post(f"{DF}/doctypes/suggest", files=[("files", ("c.pdf", pdf([[
        "Delivery Note", "Note No: DN-55", "Shipped On: 03/11/2026", "Weight: 120.50", "Driver: Saeed"]]), "application/pdf"))]).json()
    types = {f["name"]: f["type"] for f in custom["fields"]}
    assert types.get("note_no") == "string" and types.get("shipped_on") == "date" and types.get("weight") == "currency"


def test_flow_validation_and_live_rules(client):
    assert client.post(f"{DF}/flows", json={"name": "x", "docTypes": []}).status_code == 400
    bad = {"name": "x", "docTypes": ["invoice"], "deliveries": [{"type": "webhook", "params": {"url": "ftp://x"}}]}
    assert client.post(f"{DF}/flows", json=bad).status_code == 400
    fid = client.post(f"{DF}/flows", json={"name": "Receipts", "docTypes": ["receipt"]}).json()["id"]
    r = client.post(f"{DF}/flows/{fid}/documents", params={"requireLive": "true"},
                    files=[("files", ("a.pdf", pdf([["Receipt"]]), "application/pdf"))])
    assert r.status_code == 400
    assert client.post(f"{DF}/flows/{fid}/live").json()["status"] == "live"
    assert client.delete(f"{DF}/doctypes/receipt").status_code == 400          # in use
    assert client.delete(f"{DF}/flows/smart-inbox").status_code == 400


def test_sla_webhook(client, monkeypatch):
    import docauto.api.app as appmod
    import docauto.docflow as dfm
    hits = []
    monkeypatch.setattr(dfm.httpx, "post", lambda url, json=None, headers=None, timeout=None:
                        hits.append(json) or type("R", (), {"status_code": 200})())
    fid = client.post(f"{DF}/flows", json={"name": "Slow", "docTypes": ["invoice"],
                                           "review": {"fieldConfidence": 0.999, "documentConfidence": 0.999,
                                                      "slaHours": 0, "slaWebhook": "http://alert.local"}}).json()["id"]
    up(client, fid, pdf([INV]))
    time.sleep(0.05)
    assert appmod.docflow().check_sla() == 1 and hits[-1]["event"] == "review.overdue"
    assert appmod.docflow().check_sla() == 0            # notified once
