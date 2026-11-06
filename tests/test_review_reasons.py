"""Why a document goes to review (structured reasons), value confidence from the value's own words, the supplier
read from the page heading, and line items rebuilt from OCR geometry."""
import pytest

from docauto.extraction import RULES_INFO, RulesExtractor, header_company
from docauto.grounding import HALLUCINATION
from docauto.models import BBox, DocumentResult, ExtractedField, FieldDef, FieldSchema, Line, Page, Word
from docauto.provider import ExtractRequest
from docauto.scoring import review_reasons, score
from docauto.tables import add_line_tables, read_table


def page(rows, conf=0.95, word_conf=None):
    """A synthetic OCR page. rows: [(y, [(x, text, height?)...])]; one OCR line per row, one word per item.
    word_conf: {word text: confidence} for words read worse than the rest."""
    words, lines = [], []
    for y, items in rows:
        idx = []
        for item in items:
            x, t = item[0], item[1]
            h = item[2] if len(item) > 2 else 0.012
            idx.append(len(words))
            words.append(Word(text=t, bbox=BBox(x=x, y=y, w=0.008 * len(t), h=h),
                              confidence=(word_conf or {}).get(t, conf)))
        ws = [words[i] for i in idx]
        confs = [w.confidence for w in ws]
        lines.append(Line(text=" ".join(w.text for w in ws), wordIndexes=idx, confidence=sum(confs) / len(confs),
                          bbox=BBox(x=ws[0].bbox.x, y=y, w=ws[-1].bbox.x + ws[-1].bbox.w - ws[0].bbox.x,
                                    h=max(w.bbox.h for w in ws))))
    return Page(pageNumber=1, width=1000, height=1400, lines=lines, words=words)


def words_of(y, text, x=0.1, h=0.012):
    out = []
    for t in text.split():
        out.append((x, t, h))
        x += 0.008 * len(t) + 0.01
    return (y, out)


def document(*pages):
    d = DocumentResult(documentId="d", pages=list(pages))
    d.rebuild_text()
    return d


def extract(d, schema):
    e = RulesExtractor()
    e.info = RULES_INFO
    d.fields = e.extract(ExtractRequest(document=d, schema=schema))
    return {f.name: f for f in d.fields}


INVOICE = FieldSchema(name="inv", fields=[
    FieldDef(name="invoice_number", label="Invoice Number", required=True),
    FieldDef(name="supplier_name", label="Supplier", fallback="header_company"),
    FieldDef(name="subtotal", type="currency", label="Subtotal", required=True),
    FieldDef(name="vat_amount", type="currency", label="VAT 15%", aliases=["VAT"]),
    FieldDef(name="total", type="currency", label="Total", required=True),
    FieldDef(name="po_number", label="PO Number", required=True),
    FieldDef(name="line_items", type="table", label="Line items", columns=[
        FieldDef(name="description", label="Description / الوصف", aliases=["Item"]),
        FieldDef(name="quantity", type="number", label="Qty / الكمية", aliases=["Quantity"]),
        FieldDef(name="unit_price", type="currency", label="Unit Price / سعر الوحدة"),
        FieldDef(name="amount", type="currency", label="Amount / المبلغ")])],
    validation=["abs(subtotal + vat_amount - total) < 0.05"])


def scan_page(word_conf=None):
    """The layout of samples/invoice_scan.png, as Tesseract reads it."""
    return page([
        words_of(0.076, "ACME Trading Co. LLC", h=0.019),
        words_of(0.117, "Tax Invoice"),
        words_of(0.148, "Invoice Number: INV-2026-0042"),
        words_of(0.235, "Customer: Oneoffice Automation"),
        (0.262, [(0.11, "Description"), (0.266, "Qty"), (0.322, "Amount")]),
        (0.290, [(0.111, "RPA"), (0.145, "licence"), (0.268, "2"), (0.317, "8,000.00")]),
        (0.317, [(0.11, "Support"), (0.176, "plan"), (0.274, "1"), (0.322, "2,000.00")]),
        words_of(0.344, "Subtotal: 10,000.00"),
        words_of(0.373, "VAT 15%: 1,500.00"),
        words_of(0.402, "Total: 11,500.00 SAR"),
    ], word_conf=word_conf)


# ------------------------------------------------------------------ review reasons
def test_reasons_say_which_field_and_why():
    d = document(scan_page())
    f = extract(d, INVOICE)
    f["invoice_number"].confidence = 0.58
    f["total"].value = 11600.0
    f["vat_amount"].validation.errors.append(HALLUCINATION)
    f["vat_amount"].validation.passed = False
    score(d, INVOICE, 0.85, 0.5, {"total": 0.999})
    by = {(r.field, r.reason): r for r in d.review.reasons}
    low = by[("invoice_number", "low_confidence")]
    assert low.message == "Confidence 58% is under the 85% needed" and low.threshold == 0.85 and low.confidence == 0.58
    assert by[("po_number", "required_missing")].message == "Required, but not found on the document"
    assert ("vat_amount", "not_grounded") in by
    assert by[("total", "low_confidence")].threshold == 0.999          # the process's own threshold for the field
    assert by[(None, "check_failed")].fields == ["subtotal", "total", "vat_amount"]
    # the one-line warnings are as before, one per field
    assert "field 'po_number' needs review" in d.warnings and "field 'invoice_number' needs review" in d.warnings
    assert d.warnings.count("field 'vat_amount' needs review") == 1


def test_document_confidence_and_restating_changes_nothing():
    d = document(scan_page())
    extract(d, INVOICE)
    score(d, INVOICE, 0.5, 0.99)
    assert [r.reason for r in d.review.reasons if r.field is None] == ["document_confidence"]
    r = d.review.reasons[-1]
    assert r.threshold == 0.99 and r.message.startswith("Document confidence ") and r.message.endswith("under the 99% needed")
    before = d.model_dump()
    again = review_reasons(d, INVOICE, 0.5, 0.99)          # a stored result: same reasons, nothing changed
    assert [x.model_dump() for x in again] == [x.model_dump() for x in d.review.reasons]
    assert d.model_dump() == before


def test_no_reasons_when_nothing_needs_a_person():
    d = document(scan_page())
    extract(d, INVOICE)
    s = INVOICE.model_copy(update={"fields": [x for x in INVOICE.fields if x.name not in ("po_number", "supplier_name")]})
    d.fields = [x for x in d.fields if x.name not in ("po_number", "supplier_name")]
    score(d, s, 0.5, 0.5)
    assert d.review.reasons == [] and not d.review.required


# ------------------------------------------------------------------ value confidence
def test_a_poorly_read_label_does_not_lower_the_value():
    """Tesseract read 'Subtotal:' at 0% and '10,000.00' at 95%: the value is 0.9 x 0.95, not 0.9 x 0.475,
    then the passing arithmetic check (subtotal + VAT = total) corroborates it."""
    d = document(scan_page(word_conf={"Subtotal:": 0.0}))
    f = extract(d, INVOICE)
    assert f["subtotal"].value == 10000.0 and f["subtotal"].confidence == pytest.approx(0.9 * 0.95)
    score(d, INVOICE, 0.85, 0.5)
    assert f["subtotal"].confidence == pytest.approx(0.99)
    assert not any(r.field == "subtotal" for r in d.review.reasons)


def test_a_poorly_read_value_stays_doubtful_and_next_line_values_use_their_line():
    d = document(scan_page(word_conf={"10,000.00": 0.4}))
    assert extract(d, INVOICE)["subtotal"].confidence == pytest.approx(0.9 * 0.4)
    d = document(page([words_of(0.1, "Total"), words_of(0.12, "1,150.00")], word_conf={"1,150.00": 0.5}))
    t = extract(d, FieldSchema(name="t", fields=[FieldDef(name="total", type="currency")]))["total"]
    assert t.value == 1150.0 and t.confidence == pytest.approx(0.9 * 0.5) and t.bbox.y == pytest.approx(0.12)


# ------------------------------------------------------------------ supplier from the page heading
def test_supplier_from_the_heading_with_modest_confidence():
    d = document(scan_page())
    s = extract(d, INVOICE)["supplier_name"]
    assert s.value == "ACME Trading Co. LLC" and s.confidence <= 0.75 and s.bbox.y == pytest.approx(0.076)
    off = FieldSchema(name="x", fields=[FieldDef(name="supplier_name", label="Supplier")])
    assert extract(document(scan_page()), off)["supplier_name"].value is None      # only when the type asks for it


def test_a_printed_supplier_label_wins_over_the_heading():
    d = document(page([words_of(0.05, "ACME Trading Co. LLC", h=0.02), words_of(0.1, "Tax Invoice"),
                       words_of(0.13, "Supplier: Gulf Office Supplies")]))
    assert extract(d, INVOICE)["supplier_name"].value == "Gulf Office Supplies"


@pytest.mark.parametrize("heading,expected", [
    (["مؤسسة النور للتجارة", "فاتورة ضريبية"], "مؤسسة النور للتجارة"),
    (["Tax Invoice", "Red Sea Print House", "Invoice Number: 1"], "Red Sea Print House"),     # below the title
    (["P.O. Box 1234, Riyadh", "Tel 011 555 1234", "Tax Invoice"], None),                     # contact lines only
    (["Invoice Number: INV-1", "Falcon IT Services LLC"], None),                              # labelled data first
    (["15/10/2026", "INV-2026-0042", "Tax Invoice"], None),                                   # numbers, no name
])
def test_heading_company_is_careful(heading, expected):
    d = document(page([words_of(0.05 + 0.03 * i, t) for i, t in enumerate(heading)]))
    hit = header_company(d)
    assert (hit[1].text if hit else None) == expected


def test_the_largest_company_line_wins():
    d = document(page([words_of(0.03, "Welcome to our store"), words_of(0.06, "Nile Logistics Ltd", h=0.02),
                       words_of(0.1, "Receipt")]))
    assert header_company(d)[1].text == "Nile Logistics Ltd"


# ------------------------------------------------------------------ line items from geometry
def test_line_items_from_ocr_geometry():
    d = document(scan_page())
    assert add_line_tables(d, INVOICE) == 1
    t = d.pages[0].tables[0]
    assert t.to_matrix() == [["Description", "Qty", "Amount"], ["RPA licence", "2", "8,000.00"],
                             ["Support plan", "1", "2,000.00"]]
    assert all(c.bbox is not None for c in t.cells) and 0 < t.confidence < 0.95
    rows = extract(d, INVOICE)["line_items"].value
    assert rows == [{"description": "RPA licence", "quantity": 2.0, "amount": 8000.0},
                    {"description": "Support plan", "quantity": 1.0, "amount": 2000.0}]
    assert add_line_tables(d, INVOICE) == 0           # a page that has tables (an engine's) is left alone


def test_unit_price_column_wrapped_description_and_totals_without_colon():
    p = page([
        (0.20, [(0.10, "Item"), (0.45, "Quantity"), (0.60, "Unit"), (0.645, "Price"), (0.80, "Amount")]),
        (0.23, [(0.10, "Annual"), (0.16, "support"), (0.47, "3"), (0.62, "500.00"), (0.80, "1,500.00")]),
        (0.25, [(0.10, "(on-site)")]),
        (0.28, [(0.10, "Tax"), (0.14, "advisory"), (0.47, "1"), (0.62, "250.00"), (0.80, "250.00")]),
        (0.31, [(0.10, "Subtotal"), (0.80, "1,750.00")]),
    ])
    t = read_table(p, INVOICE.fields[-1])
    assert t.to_matrix() == [["Item", "Quantity", "Unit Price", "Amount"],
                             ["Annual support (on-site)", "3", "500.00", "1,500.00"],
                             ["Tax advisory", "1", "250.00", "250.00"]]


def test_arabic_table_header():
    p = page([
        (0.20, [(0.70, "الوصف"), (0.45, "الكمية"), (0.15, "المبلغ")]),
        (0.23, [(0.70, "رخصة"), (0.47, "2"), (0.15, "8,000.00")]),
        (0.26, [(0.70, "الإجمالي:"), (0.15, "8,000.00")]),
    ])
    t = read_table(p, INVOICE.fields[-1])
    assert t.to_matrix() == [["المبلغ", "الكمية", "الوصف"], ["8,000.00", "2", "رخصة"]]
    rows = extract(document(p.model_copy(update={"tables": [t]})), INVOICE)["line_items"].value
    assert rows == [{"description": "رخصة", "quantity": 2.0, "amount": 8000.0}]


def test_no_header_no_table():
    p = page([words_of(0.1, "Description of services rendered"), words_of(0.12, "Total Amount: 1,000.00")])
    assert read_table(p, INVOICE.fields[-1]) is None
