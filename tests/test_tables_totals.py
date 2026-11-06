"""The line that ends a table's items (tables._TOTALS), including the OCR slips Tesseract makes on some versions."""
import pytest

from docauto.tables import _TOTALS


@pytest.mark.parametrize("text", ["Subtotal: 10,000", "Subtota]: 10,000", "Sub-total 5", "Tota1 15.00", "TOTAL",
                                  "Grand Total: 9", "Net tota| 3", "VAT 15%", "Amount due 100", "المجموع 100"])
def test_ends_the_items(text):
    assert _TOTALS.match(text)


@pytest.mark.parametrize("text", ["RPA licence 2 4,000 8,000", "Totally new item 1 5", "Taxi fare 1 20", "Description"])
def test_does_not_end_the_items(text):
    assert not _TOTALS.match(text)
