"""Field grounding (page + box of each value, hallucination guard) and per-field review thresholds."""
import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLES, make_config
from docauto.grounding import HALLUCINATION, as_number, ground_document, locations
from docauto.models import BBox, DocumentResult, ExtractedField, FieldDef, FieldSchema, FieldValidation, Line, Page, Word
from docauto.scoring import score

DF = "/api/docs/v1/df"


def page(lines, words=True):
    """A synthetic OCR page: one word box per space-separated token, lines stacked top to bottom."""
    ws, ls, y = [], [], 0.05
    for t in lines:
        idx, x = [], 0.05
        for w in t.split():
            idx.append(len(ws))
            ws.append(Word(text=w, bbox=BBox(x=x, y=y, w=0.01 * len(w), h=0.02), confidence=0.9))
            x += 0.01 * len(w) + 0.01
        ls.append(Line(text=t, bbox=BBox(x=0.05, y=y, w=x - 0.05, h=0.02), wordIndexes=idx if words else [],
                       confidence=0.9))
        y += 0.04
    return Page(pageNumber=1, width=1, height=1, lines=ls, words=ws if words else [])


def doc(lines, fields, words=True):
    d = DocumentResult(documentId="d", pages=[page(lines, words)])
    d.rebuild_text()
    d.fields = fields
    return d


def same(a, b):
    return all(abs(getattr(a, k) - getattr(b, k)) < 1e-9 for k in "xywh")


def f(name, value, ftype="string", conf=0.9, **kw):
    return ExtractedField(name=name, type=ftype, value=value, confidence=conf, **kw)


def test_numbers_in_any_style():
    assert as_number("SAR 11,500.00") == as_number("١١٬٥٠٠٫٠٠") == as_number("11.500,00 ر.س") == 11500.0
    assert as_number("1 500.00") == 1500.0 and as_number("INV-2026") is None


def test_values_are_found_and_boxed():
    d = doc(["Invoice Number: INV-2026-0042", "التاريخ: ١٥/١٠/٢٠٢٦", "Supplier: ACME Trading Co. LLC",
             "Subtotal: 10,000.00", "Total: ١١٬٥٠٠٫٠٠ SAR", "VAT No 35075291 7034233"],
            [f("invoice_number", "INV-2026-0042"), f("invoice_date", "2026-10-15", "date"),
             f("supplier_name", "acme trading co llc"), f("total", 11500.0, "currency"),
             f("subtotal", "10000", "currency"), f("vat_no", "350752917034233"), f("missing", "PO-999")])
    ground_document(d)
    by = {x.name: x for x in d.fields}
    for name in ("invoice_number", "invoice_date", "supplier_name", "total", "subtotal", "vat_no"):
        assert by[name].grounded is True and by[name].groundedBy == "ocr" and by[name].page == 1, name
    words = d.pages[0].words
    assert same(by["invoice_number"].bbox, words[2].bbox)                      # the value's word, not the label
    assert same(by["total"].bbox, words[13].bbox)                              # Arabic-Indic digits, without "SAR"
    assert abs(by["supplier_name"].bbox.w - (words[9].bbox.x + words[9].bbox.w - words[6].bbox.x)) < 1e-9
    assert by["missing"].grounded is False and by["missing"].confidence == 0.9   # rules values: no penalty
    assert d.fields[0].validation.passed


def test_small_ocr_slips_and_lines_without_words():
    d = doc(["Supplier: ACME Tradlng Co. LLC", "Total 1,150.00"], [f("supplier_name", "ACME Trading Co. LLC"),
                                                                    f("total", 1150.0, "currency")], words=False)
    ground_document(d)
    assert all(x.grounded for x in d.fields)
    assert same(d.fields[1].bbox, d.pages[0].lines[1].bbox)                    # no words: the line's box


def test_hint_from_the_extractor_wins():
    d = doc(["Subtotal: 100.00", "Total: 100.00"], [f("total", 100.0, "currency", page=1,
                                                      bbox=BBox(x=0.05, y=0.09, w=0.2, h=0.02))])
    ground_document(d)
    assert d.fields[0].bbox.y == pytest.approx(0.09)


def test_ai_guard_penalises_unfound_and_restores_formatted():
    err = FieldValidation(passed=False, errors=[HALLUCINATION])
    d = doc(["Invoice Date: 15/10/2026", "Total: 1,150.00"],
            [f("invoice_date", "2026-10-15", "date", conf=0.4, validation=err, providerId="claude"),
             f("supplier", "Imaginary Corp", conf=0.8, providerId="claude"),
             f("total", 1150.0, "currency", conf=0.9, providerId="claude")])
    ground_document(d, ai=True)
    date, supplier, total = d.fields
    assert date.grounded and date.validation.passed and date.confidence == 0.8   # date printed another way
    assert supplier.grounded is False and supplier.confidence == 0.4
    assert supplier.validation.errors == [HALLUCINATION] and not supplier.validation.passed
    assert total.grounded and total.confidence == 0.9


def test_table_rows_and_cells():
    rows = [{"description": "RPA licence", "quantity": 2, "amount": 8000.0},
            {"description": "Support plan", "quantity": 1, "amount": 2000.0},
            {"description": "Not printed", "quantity": 9, "amount": 99.0}]
    d = doc(["Description Qty Amount", "RPA licence 2 8,000.00", "Support plan 1 2,000.00", "Total: 10,000.00"],
            [f("line_items", rows, "table")])
    schema = FieldSchema(name="x", fields=[FieldDef(name="line_items", type="table", columns=[
        FieldDef(name="description"), FieldDef(name="quantity", type="number"), FieldDef(name="amount", type="currency")])])
    ground_document(d, schema)
    fld = d.fields[0]
    r0, r1, r2 = fld.rowLocations
    assert r0.grounded and r0.page == 1 and r0.bbox.y == pytest.approx(0.09) and set(r0.cells) == {"description", "quantity", "amount"}
    assert r1.grounded and r1.bbox.y == pytest.approx(0.13) and r1.cells["amount"].bbox.x > r1.cells["description"].bbox.x
    assert not r2.grounded and r2.page is None
    assert fld.grounded is False and fld.page == 1                       # one row was not found
    loc = locations(d)["line_items"]
    assert loc["rows"][1]["cells"]["quantity"]["grounded"] is True


def test_per_field_thresholds_override_the_process_value():
    schema = FieldSchema(name="x", fields=[FieldDef(name="a"), FieldDef(name="b", confidenceThreshold=0.5),
                                           FieldDef(name="c")])

    def run(over):
        d = DocumentResult(documentId="d", fields=[f("a", "x", conf=0.8), f("b", "y", conf=0.8), f("c", "z", conf=0.8)])
        score(d, schema, 0.85, 0.0, over)
        return {w.split("'")[1] for w in d.warnings if w.startswith("field")}

    assert run(None) == {"a", "c"}                                       # b: the document type's own 0.5
    assert run({"a": 0.7, "b": 0.95}) == {"b", "c"}                      # process per-field values win


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DOCAUTO_FOLDER_WATCHER", "false")
    monkeypatch.delenv("DOCAUTO_PLATFORM_URL", raising=False)
    monkeypatch.delenv("DOCAUTO_API_TOKEN", raising=False)
    import docauto.api.app as appmod
    from docauto.pipeline import DocumentService
    appmod.set_service(DocumentService(make_config()))
    return TestClient(appmod.app)


def test_flow_field_thresholds_get_put_and_validation(client):
    r = client.post(f"{DF}/flows", json={"name": "AP", "docTypes": ["invoice"],
                                         "review": {"fieldThresholds": {"total": 0.95, "po_number": 0.5}}})
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    assert client.get(f"{DF}/flows/{fid}").json()["review"]["fieldThresholds"] == {"total": 0.95, "po_number": 0.5}
    review = client.get(f"{DF}/flows/{fid}").json()["review"]
    r = client.put(f"{DF}/flows/{fid}", json={"review": {**review, "fieldThresholds": {"total": 1.5}}})
    assert r.status_code == 400 and "between 0 and 1" in r.text
    r = client.put(f"{DF}/flows/{fid}", json={"review": {**review, "fieldThresholds": {"bad name": 0.5}}})
    assert r.status_code == 400
    r = client.put(f"{DF}/flows/{fid}", json={"review": {**review, "fieldThresholds": {"total": "high"}}})
    assert r.status_code == 400
    r = client.put(f"{DF}/flows/{fid}", json={"review": {**review, "fieldThresholds": {"total": 0.999}}})
    assert r.status_code == 200 and r.json()["review"]["fieldThresholds"] == {"total": 0.999}
    # the digital sample: total is read exactly (0.99 at most) so a 0.999 threshold sends it to review
    up = client.post(f"{DF}/flows/{fid}/documents", params={"wait": "true"},
                     files=[("files", ("inv.pdf", (SAMPLES / "invoice_digital.pdf").read_bytes(), "application/pdf"))])
    d = client.get(f"{DF}/documents/{up.json()['documents'][0]['documentId']}").json()
    assert d["status"] == "validation"
    assert any("field 'total' needs review" in w for w in
               client.get(f"/api/docs/v1/documents/{d['id']}/result").json()["warnings"])


def test_api_exposes_locations(client):
    fid = client.post(f"{DF}/flows", json={"name": "AP", "docTypes": ["invoice"]}).json()["id"]
    up = client.post(f"{DF}/flows/{fid}/documents", params={"wait": "true"},
                     files=[("files", ("inv.pdf", (SAMPLES / "invoice_digital.pdf").read_bytes(), "application/pdf"))])
    doc_id = up.json()["documents"][0]["documentId"]
    d = client.get(f"{DF}/documents/{doc_id}").json()
    assert d["fields"]["invoice_number"] == "INV-2026-0042"                  # existing keys unchanged
    loc = d["locations"]["invoice_number"]
    assert loc["grounded"] and loc["groundedBy"] == "ocr" and loc["page"] == 1
    assert set(loc["bbox"]) == {"x", "y", "w", "h"} and 0 < loc["bbox"]["w"] < 0.5
    res = client.get(f"/api/docs/v1/documents/{doc_id}/result").json()
    total = next(x for x in res["fields"] if x["name"] == "total")
    assert total["grounded"] is True and total["page"] == 1 and total["bbox"]
    assert client.get(f"/api/docs/v1/documents/{doc_id}/fields").json()["locations"]["total"]["grounded"]
