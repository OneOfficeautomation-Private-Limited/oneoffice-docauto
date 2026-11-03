"""Built-in rules extractor, coercion, safe rule evaluation, scoring."""
import pytest

from docauto.extraction import RULES_INFO, RulesExtractor, coerce, safe_eval
from docauto.models import BBox, DocumentResult, FieldDef, FieldSchema, KeyValue, Line, Page
from docauto.provider import ExtractRequest
from docauto.scoring import score


def doc_from_lines(*lines, conf=0.95, kvs=()):
    p = Page(pageNumber=1, width=100, height=100,
             lines=[Line(text=t, bbox=BBox(x=0, y=i / 20, w=0.5, h=0.04), confidence=conf) for i, t in enumerate(lines)],
             keyValuePairs=list(kvs))
    d = DocumentResult(documentId="d", pages=[p]); d.rebuild_text()
    return d


def extractor():
    e = RulesExtractor(); e.info = RULES_INFO
    return e


@pytest.mark.parametrize("raw,t,expected", [
    ("1,234.50 SAR", "currency", 1234.5), ("١٬٢٣٤", "number", None), ("12,5", "number", 12.5),
    ("15/10/2026", "date", "2026-10-15"), ("2026-01-31", "date", "2026-01-31"), ("نعم", "boolean", True),
    ("٣٠٠", "number", 300.0),
])
def test_coerce(raw, t, expected):
    val, ok = coerce(raw, t)
    if expected is None:
        return
    assert ok and val == expected


def test_specific_label_wins_and_word_boundaries():
    d = doc_from_lines("Supplier VAT No: 300123456700003", "Subtotal: 100.00", "VAT 15%: 15.00", "Total: 115.00")
    s = FieldSchema(name="inv", fields=[FieldDef(name="vat_amount", type="currency", label="VAT 15%", aliases=["VAT"]),
                                        FieldDef(name="total", type="currency", label="Total"),
                                        FieldDef(name="subtotal", type="currency")])
    got = {f.name: f.value for f in extractor().extract(ExtractRequest(document=d, schema=s))}
    assert got == {"vat_amount": 15.0, "total": 115.0, "subtotal": 100.0}


def test_fuzzy_label_and_kv_priority():
    d = doc_from_lines("Subtota]: 10,000.00", kvs=[KeyValue(key="Invoice No", value="X-9", confidence=0.9)])
    s = FieldSchema(name="i", fields=[FieldDef(name="subtotal", type="currency"),
                                      FieldDef(name="invoice_no", label="Invoice No")])
    got = {f.name: f for f in extractor().extract(ExtractRequest(document=d, schema=s))}
    assert got["subtotal"].value == 10000.0
    assert got["invoice_no"].value == "X-9"


def test_value_on_next_line_and_arabic_label():
    d = doc_from_lines("الإجمالي", "1,150.00")
    s = FieldSchema(name="i", fields=[FieldDef(name="total", type="currency", label="Total / الإجمالي")])
    assert extractor().extract(ExtractRequest(document=d, schema=s))[0].value == 1150.0


def test_safe_eval_rules_and_rejects_code():
    v = {"subtotal": 100.0, "vat": 15.0, "total": 115.0, "items": [{"amount": 60.0}, {"amount": 40.0}]}
    assert safe_eval("abs(subtotal + vat - total) < 0.01", v)
    assert safe_eval("sum(items.amount) == subtotal", v)
    with pytest.raises(ValueError):
        safe_eval("__import__('os').system('echo hi')", v)


def test_scoring_review_and_corroboration():
    d = doc_from_lines("Subtotal: 100.00", "VAT: 15.00", "Total: 115.00")
    s = FieldSchema(name="i", fields=[FieldDef(name="subtotal", type="currency", required=True),
                                      FieldDef(name="vat", type="currency"),
                                      FieldDef(name="total", type="currency", required=True),
                                      FieldDef(name="po_number", required=True)],
                    validation=["abs(subtotal + vat - total) < 0.01"])
    d.fields = extractor().extract(ExtractRequest(document=d, schema=s))
    score(d, s, 0.85, 0.5)
    f = {x.name: x for x in d.fields}
    assert f["total"].confidence > 0.95           # corroborated by the passing rule
    assert "required field missing" in f["po_number"].validation.errors
    assert d.review.required and any("po_number" in w for w in d.warnings)


def test_classify_keywords():
    d = doc_from_lines("TAX INVOICE", "Invoice number 1", "Total due")
    c = extractor().classify(d, ["invoice", "bank statement"])
    assert c.label == "invoice"
