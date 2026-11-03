"""Contract tests: every mapper/engine must produce a valid, normalized DocumentResult."""
import json
import shutil

import pytest

from conftest import fixture_json, load_option
from docauto.models import DocumentResult


def assert_contract(doc: DocumentResult):
    # round-trips through JSON schema of the model
    DocumentResult.model_validate_json(doc.model_dump_json())
    assert doc.pages, "at least one page"
    for p in doc.pages:
        assert p.width > 0 and p.height > 0
        for item in [*p.words, *p.lines]:
            b = item.bbox
            assert 0 <= b.x <= 1 and 0 <= b.y <= 1 and 0 <= b.w <= 1 and 0 <= b.h <= 1, b
            assert item.confidence is None or 0 <= item.confidence <= 1
        for t in p.tables:
            assert t.rows > 0 and t.cols > 0
            assert all(c.row < t.rows and c.col < t.cols for c in t.cells)
    assert doc.text


def test_azure_mapping():
    mod = load_option("tier2_cloud/azure_di/provider.py")
    doc = mod.map_azure(fixture_json("azure_layout.json"), "d1")
    assert_contract(doc)
    p = doc.pages[0]
    assert p.lines[0].text == "Invoice Number: INV-1"
    assert p.lines[0].confidence == pytest.approx((0.99 + 0.98 + 0.90) / 3, abs=1e-3)
    assert p.lines[0].bbox.x == pytest.approx(0.1) and p.lines[0].bbox.y == pytest.approx(0.05)
    assert p.tables[0].to_matrix() == [["Item", "Amount"], ["A", "100.00"]]
    assert p.tables[0].cells[0].isHeader
    assert p.keyValuePairs[0].key == "Total:" and p.keyValuePairs[0].value == "115.00"
    assert p.blocks[0].type == "title"


def test_textract_mapping():
    mod = load_option("tier2_cloud/aws_textract/provider.py")
    page = mod.map_textract_page(fixture_json("textract_blocks.json")["Blocks"], 1, 1000, 2000)
    doc = DocumentResult(documentId="d", pages=[page]); doc.rebuild_text()
    assert_contract(doc)
    assert page.lines[0].text == "Total: 115.00" and page.lines[0].confidence == pytest.approx(0.98)
    assert page.lines[0].wordIndexes == [0, 1]
    assert page.keyValuePairs[0].key == "Total" and page.keyValuePairs[0].value == "115.00"
    assert page.keyValuePairs[0].confidence == pytest.approx(0.88)
    assert page.tables[0].to_matrix() == [["Total:"], ["115.00"]]


def test_docai_mapping():
    mod = load_option("tier2_cloud/google_docai/provider.py")
    doc = mod.map_docai(fixture_json("docai_document.json"), "d")
    assert_contract(doc)
    p = doc.pages[0]
    assert p.lines[0].text == "Total: 115.00"
    assert p.words[0].text == "Total:"
    assert p.tables[0].to_matrix() == [["Item", "Amount"], ["A", "100.00"]]
    assert p.keyValuePairs[0].key == "Total" and p.keyValuePairs[0].value == "115.00"


def test_abbyy_template_mapper():
    mod = load_option("tier2_cloud/abbyy_cloud/provider.py")
    page = mod.map_abbyy_words([{"text": "Total 5", "left": 10, "top": 20, "right": 110, "bottom": 40,
                                 "confidence": 80}], 1, 1000, 1000)
    assert page.lines[0].confidence == 0.8 and page.lines[0].bbox.w == pytest.approx(0.1)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_tesseract_engine(samples):
    from PIL import Image
    from docauto.provider import ProviderContext, RecognizeRequest, Capability
    mod = load_option("tier1_opensource/tesseract/provider.py")
    prov = mod.TesseractProvider()
    prov.info = type("I", (), {"id": "tesseract"})()
    prov.init(ProviderContext(config={}, credentials={}))
    assert prov.health_check().ok
    img = Image.open(samples / "screen.png").convert("RGB")
    doc = prov.recognize(RecognizeRequest("d", [img], ["en"], [Capability.SCREEN_OCR]))
    assert_contract(doc)
    assert "C-10293" in doc.text
