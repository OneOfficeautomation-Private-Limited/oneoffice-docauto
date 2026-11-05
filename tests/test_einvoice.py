"""E-invoices: UBL XML (alone or inside a PDF/A-3) instead of OCR; ZATCA / India GST QR codes cross-checking fields."""
import base64
import io
import json

import pytest
from fastapi.testclient import TestClient

from conftest import FIX, make_config
from docauto import einvoice
from docauto.models import BBox, DocumentResult, ExtractedField, FieldDef, FieldSchema

DF = "/api/docs/v1/df"
XML = (FIX / "zatca_invoice.xml").read_bytes()
LINES = ["ACME Trading Co. LLC", "Tax Invoice", "Invoice Number: INV-2026-0042", "Invoice Date: 15/10/2026",
         "Supplier VAT No: 300123456700003", "Customer: Oneoffice Automation", "Subtotal: 1,000.00",
         "VAT 15%: 150.00", "Total: 1,150.00 SAR"]


def tlv(tags, ber=False):
    out = b""
    for t, v in tags:
        v = v if isinstance(v, bytes) else v.encode()
        out += bytes([t]) + (bytes([0x81, len(v)]) if ber and len(v) > 127 else bytes([len(v)])) + v
    return base64.b64encode(out).decode()


def zatca(total="1150.00", vat="150.00", name="ACME Trading Co. LLC"):
    return tlv([(1, name), (2, "300123456700003"), (3, "2026-10-15T10:30:00Z"), (4, total), (5, vat)])


def gst_jwt(data):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'RS256', 'typ': 'JWT'})}.{enc({'data': json.dumps(data), 'iss': 'NIC'})}.c2lnbmF0dXJl"


def qr_image(text, module=6):
    import cv2
    from PIL import Image
    q = cv2.QRCodeEncoder.create().encode(text)
    q = cv2.resize(q, (q.shape[1] * module, q.shape[0] * module), interpolation=cv2.INTER_NEAREST)
    q = cv2.copyMakeBorder(q, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)
    return Image.fromarray(q).convert("RGB")


def invoice_pdf(lines=LINES, qr=None, xml=None):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for t in lines:
        c.setFont("Helvetica", 12)
        c.drawString(60, y, t)
        y -= 22
    if qr:
        c.drawImage(ImageReader(qr_image(qr)), 400, 640, 150, 150)
    c.showPage()
    c.save()
    data = buf.getvalue()
    if xml:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        pdf.new_attachment("invoice.xml").set_data(xml)
        out = io.BytesIO()
        pdf.save(out)
        data = out.getvalue()
    return data


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DOCAUTO_FOLDER_WATCHER", "false")
    monkeypatch.delenv("DOCAUTO_PLATFORM_URL", raising=False)
    monkeypatch.delenv("DOCAUTO_API_TOKEN", raising=False)
    import docauto.api.app as appmod
    from docauto.pipeline import DocumentService
    appmod.set_service(DocumentService(make_config()))
    return TestClient(appmod.app)


def flow(client, **extra):
    r = client.post(f"{DF}/flows", json={"name": "AP invoices", "docTypes": ["invoice"], **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def up(client, fid, data, name):
    r = client.post(f"{DF}/flows/{fid}/documents", params={"wait": "true"}, files=[("files", (name, data, "application/octet-stream"))])
    assert r.status_code == 202, r.text
    return client.get(f"{DF}/documents/{r.json()['documents'][0]['documentId']}").json()


def result(client, doc_id):
    return client.get(f"/api/docs/v1/documents/{doc_id}/result").json()


# ------------------------------------------------------------------ UBL XML
def test_parse_zatca_ubl():
    inv = einvoice.parse_ubl(XML)
    v = inv["values"]
    assert v["invoice_number"] == "INV-2026-0042" and v["invoice_date"] == "2026-10-15"
    assert v["supplier_name"] == "ACME Trading Co. LLC" and v["supplier_vat"] == "300123456700003"
    assert v["customer_name"] == "Oneoffice Automation" and v["customer_vat"] == "310987654300003"
    assert (v["currency"], v["subtotal"], v["vat_amount"], v["total"]) == ("SAR", 10000.0, 1500.0, 11500.0)
    assert v["po_number"] == "PO-7781"
    assert v["line_items"][0] == {"description": "RPA licence", "quantity": 2.0, "unit_price": 4000.0, "amount": 8000.0,
                                  "vat_amount": 1200.0}
    assert inv["raw"]["uuid"].startswith("3cf5ee18") and inv["raw"]["qr"]["total"] == 11500.0


def test_not_an_einvoice():
    assert einvoice.parse_ubl(b"<note><to>x</to></note>") is None
    assert einvoice.parse_ubl(b"not xml") is None
    evil = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'
    assert einvoice.parse_ubl(evil) is None


def test_xml_upload_needs_no_ocr(client):
    d = up(client, flow(client), XML, "INV-2026-0042.xml")
    assert d["status"] == "success", d
    assert d["fields"]["total"] == 11500.0 and d["fields"]["supplier_vat"] == "300123456700003"
    assert d["fields"]["customer_vat"] == "310987654300003"
    assert len(d["fields"]["line_items"]) == 2 and d["fields"]["line_items"][1]["description"] == "Support plan"
    assert set(d["confidence"].values()) <= {1.0, 0.0} and d["confidence"]["total"] == 1.0
    assert d["locations"]["total"] == {"page": None, "bbox": None, "grounded": True, "groundedBy": "einvoice-xml"}
    res = result(client, d["id"])
    assert [s["stage"] for s in res["provenance"]["steps"]] == ["preprocess", "extract"]
    assert res["provenance"]["steps"][1]["providerId"] == "einvoice-xml" and res["einvoice"]["source"] == "einvoice-xml"
    assert next(f for f in res["fields"] if f["name"] == "total")["providerId"] == "einvoice-xml"


def test_xml_in_the_inbox_goes_to_the_invoice_type(client):
    d = up(client, "smart-inbox", XML, "e-invoice.xml")
    assert d["status"] == "success" and d["doctype_id"] == "invoice"


def test_pdf_a3_with_embedded_xml(client):
    d = up(client, flow(client), invoice_pdf(xml=XML), "invoice.pdf")
    res = result(client, d["id"])
    assert len(res["pages"]) == 1 and res["source"]["pageCount"] == 1
    assert [s["providerId"] for s in res["provenance"]["steps"]] == ["core", "pdf-text", "einvoice-xml"]
    # values come from the XML (the page says 1,150.00; the XML 11,500.00) ...
    assert d["fields"]["total"] == 11500.0 and res["einvoice"]["raw"]["attachment"] == "invoice.xml"
    # ... and are highlighted where the text layer prints them
    loc = d["locations"]["invoice_number"]
    assert loc["grounded"] and loc["groundedBy"] == "ocr" and loc["page"] == 1 and 0 < loc["bbox"]["x"] < 1
    assert d["locations"]["total"]["groundedBy"] == "einvoice-xml"


def test_einvoice_first_off(client):
    fid = flow(client, intake={"einvoiceFirst": False})
    assert client.get(f"{DF}/flows/{fid}").json()["intake"]["einvoiceFirst"] is False
    assert up(client, fid, XML, "x.xml")["status"] == "failed"          # XML is not a page to read
    d = up(client, fid, invoice_pdf(xml=XML), "invoice.pdf")
    assert d["fields"]["total"] == 1150.0                                 # read from the page instead


# ------------------------------------------------------------------ QR payloads
def test_zatca_tlv():
    qr = einvoice.parse_zatca_qr(zatca())
    assert qr["source"] == "zatca-qr"
    assert qr["values"] == {"supplier_name": "ACME Trading Co. LLC", "supplier_vat": "300123456700003", "total": 1150.0,
                            "vat_amount": 150.0, "invoice_date": "2026-10-15"}
    # phase 2: tags 6-9 (hash, signature, key, certificate signature) are kept raw; long values with BER lengths
    long_name = "شركة " * 15
    qr2 = einvoice.parse_zatca_qr(tlv([(1, long_name), (2, "300123456700003"), (3, "2026-10-15T10:30:00"),
                                       (4, "1150.00"), (5, "150.00"), (6, b"\x01\x02"), (8, b"k" * 88)], ber=True))
    assert qr2["values"]["supplier_name"] == long_name.strip() and qr2["raw"]["tags"]["6"] == "AQI="
    assert einvoice.parse_zatca_qr("https://example.com") is None
    assert einvoice.parse_zatca_qr(base64.b64encode(b"\x01\x03abc").decode()) is None   # tags missing


def test_gst_jwt_payload_without_signature_check():
    qr = einvoice.parse_qr(gst_jwt({"SellerGstin": "29AAGCB1286Q000", "BuyerGstin": "27AAACR5055K1Z7", "DocNo": "DOC/001",
                                     "DocTyp": "INV", "DocDt": "05/11/2026", "TotInvVal": 118000.5, "ItemCnt": 1,
                                     "MainHsnCode": "998431", "Irn": "a5c12dca80e743321740b001fd70953e8738d109865d28ba4013750f2046f229"}))
    assert qr["source"] == "gst-qr"
    v = qr["values"]
    assert v["supplier_gstin"] == "29AAGCB1286Q000" and v["invoice_number"] == "DOC/001"
    assert v["invoice_date"] == "2026-11-05" and v["total"] == 118000.5 and v["irn"].startswith("a5c1")
    assert qr["raw"]["MainHsnCode"] == "998431"
    assert einvoice.parse_gst_qr("a.b.c") is None


# ------------------------------------------------------------------ cross-check
SCHEMA = FieldSchema(name="Invoice", fields=[FieldDef(name="invoice_number"), FieldDef(name="invoice_date", type="date"),
                                             FieldDef(name="supplier_name"), FieldDef(name="supplier_vat"),
                                             FieldDef(name="vat_amount", type="currency"), FieldDef(name="total", type="currency")])


def doc_with(**values):
    d = DocumentResult(documentId="d")
    d.fields = [ExtractedField(name=k, value=v, confidence=0.7, providerId="claude", type="currency" if isinstance(v, float) else "string")
                for k, v in values.items()]
    return d


def test_cross_check_confirms_flags_and_fills():
    qr = {**einvoice.parse_zatca_qr(zatca(total="1150.00")), "page": 1, "bbox": BBox(x=0.7, y=0.1, w=0.2, h=0.2)}
    d = doc_with(total=1510.0, vat_amount=150.0, supplier_name="ACME Trading Co LLC")
    d.fields[1].validation.errors = ["value not found in OCR text (possible hallucination)"]
    d.fields[1].validation.passed = False
    checks = {c["field"]: c["result"] for c in einvoice.cross_check(d, qr, SCHEMA)}
    by = {f.name: f for f in d.fields}
    assert checks == {"total": "mismatch", "vat_amount": "match", "supplier_name": "match", "supplier_vat": "filled",
                      "invoice_date": "filled"}
    assert by["total"].validation.errors == ["QR code says total 1,150.00; page reads 1,510.00"]
    assert not by["total"].validation.passed and by["total"].confidence <= 0.5
    assert by["vat_amount"].validation.passed and by["vat_amount"].confidence == 0.99           # QR confirms the AI
    assert by["vat_amount"].grounded and by["vat_amount"].groundedBy == "qr" and by["vat_amount"].page == 1
    assert by["supplier_vat"].value == "300123456700003" and by["supplier_vat"].providerId == "zatca-qr"
    assert by["invoice_date"].value == "2026-10-15" and d.einvoice["checks"][0]["field"]


def test_gstin_fills_vat_field_when_type_has_no_gstin():
    qr = {**einvoice.parse_qr(gst_jwt({"SellerGstin": "29AAGCB1286Q000", "DocNo": "DOC/001", "TotInvVal": 100})), "page": 1}
    d = doc_with(invoice_number="DOC/001")
    einvoice.cross_check(d, qr, SCHEMA)
    by = {f.name: f for f in d.fields}
    assert by["supplier_vat"].value == "29AAGCB1286Q000" and by["total"].value == 100.0
    assert by["invoice_number"].confidence == 0.99


# ------------------------------------------------------------------ QR codes read from the page (OpenCV)
def test_opencv_reads_a_zatca_code():
    # OpenCV 4.10's own encoder makes codes from version 7 up that its decoder cannot read back, so long codes
    # (GST JWTs, ZATCA phase 2) are only tested through their parsers above
    from docauto.barcodes import read_qr
    from PIL import Image
    page = Image.new("RGB", (2480, 3508), "white")
    page.paste(qr_image(zatca(), module=7), (1800, 300))
    codes = read_qr(page)
    assert len(codes) == 1 and einvoice.parse_qr(codes[0].text)["values"]["total"] == 1150.0
    assert 0.72 < codes[0].bbox.x < 0.76 and 0.08 < codes[0].bbox.y < 0.11


def test_qr_on_the_page_confirms_the_fields(client):
    d = up(client, flow(client), invoice_pdf(qr=zatca()), "invoice.pdf")
    assert d["status"] == "success", d
    checks = {c["field"]: c["result"] for c in d["einvoice"]["checks"]}
    assert checks["total"] == checks["vat_amount"] == checks["supplier_vat"] == "match"
    assert d["confidence"]["total"] >= 0.99
    res = result(client, d["id"])
    assert res["pages"][0]["barcodes"][0]["type"] == "qr"


def test_qr_mismatch_sends_to_review(client):
    lines = [l.replace("Total: 1,150.00", "Total: 1,510.00") for l in LINES]
    d = up(client, flow(client), invoice_pdf(lines, qr=zatca()), "invoice.pdf")
    assert d["status"] == "validation"
    res = result(client, d["id"])
    total = next(f for f in res["fields"] if f["name"] == "total")
    assert "QR code says total 1,150.00; page reads 1,510.00" in total["validation"]["errors"]


def test_barcodes_capability_is_served_by_the_core(client):
    import docauto.api.app as appmod
    from docauto.pipeline import ProcessOptions
    res = appmod.service().process_bytes(invoice_pdf(qr=zatca()), "a.pdf",
                                         ProcessOptions(capabilities=["ocr_printed", "barcodes"]))
    assert res.pages[0].barcodes and einvoice.parse_qr(res.pages[0].barcodes[0].text)
