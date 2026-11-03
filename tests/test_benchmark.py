"""Accuracy benchmark: metrics, synthetic test set, runner, report and CLI."""
import json
import shutil
import sys

import pytest

from conftest import SAMPLES, make_config
from docauto import benchmark as bm


def _gen():
    sys.path.insert(0, str(SAMPLES))
    import make_benchmark_set
    return make_benchmark_set


def _service():
    from docauto.pipeline import DocumentService
    return DocumentService(make_config())


def test_char_accuracy_ignores_layout_whitespace():
    assert bm.char_accuracy("Total:   11,500.00\n\nSAR", "Total: 11,500.00 SAR") == 1.0
    assert bm.char_accuracy("Tota1: 11,500.00 SAR", "Total: 11,500.00 SAR") == pytest.approx(1 - 1 / 20)
    assert bm.char_accuracy("", "abc") == 0.0
    assert bm.char_accuracy("anything", "") is None


def test_field_correct_by_type():
    assert bm.field_correct("currency", 11500.0, 11500)
    assert bm.field_correct("currency", "11,500.00 SAR", 11500.0)
    assert not bm.field_correct("currency", 11500.01, 11500.0)
    assert bm.field_correct("date", "2026-10-15", "15/10/2026")
    assert not bm.field_correct("date", "2028-10-15", "2026-10-15")
    assert bm.field_correct("string", " inv-2026-0042 ", "INV-2026-0042")
    assert not bm.field_correct("string", None, "INV-2026-0042")
    assert bm.field_correct("string", None, None)


def test_p95_nearest_rank():
    assert bm.p95([]) is None
    assert bm.p95([5.0]) == 5.0
    assert bm.p95(list(range(1, 21))) == 19
    assert bm.p95(list(range(1, 101))) == 95


def _row(ok=True, review=False, cloud=0, tags=("scan",)):
    return {"document": "d", "engine": "e", "tags": list(tags), "seconds": 1.0, "pages": 1, "error": None,
            "char_accuracy": 0.9, "ocr_confidence": 0.8, "low_confidence_pages": 1,
            "fields": {"total": {"want": 1.0, "got": 1.0 if ok else 2.0, "ok": ok}},
            "review_required": review, "straight_through": ok and not review,
            "silent_error": not review and not ok, "cloud_pages": cloud, "credits": 0}


def test_summary_separates_straight_through_from_silent_errors():
    s = bm.summarize([_row(), _row(ok=False), _row(ok=False, review=True), _row(cloud=1, tags=("photo",))])
    o = s["overall"]
    assert o["straight_through"] == 0.5 and o["silent_errors"] == 0.25 and o["review_rate"] == 0.25
    assert o["field_accuracy"] == 0.5 and o["cloud_share"] == 0.25
    assert s["by_tag"]["photo"]["documents"] == 1 and s["by_field"]["total"] == 0.5


def test_synthetic_set_is_reproducible(tmp_path):
    pytest.importorskip("reportlab")
    g = _gen()
    for d in ("a", "b"):
        g.make_set(tmp_path / d, count=2, variants=("pdf",))
    truths = [sorted(p.read_text(encoding="utf-8") for p in (tmp_path / d).glob("*.truth.json")) for d in "ab"]
    assert len(truths[0]) == 2 and truths[0] == truths[1]
    with pytest.raises(ValueError):
        g.make_set(tmp_path / "c", count=1, variants=("polaroid",))


def test_pdf_set_runs_end_to_end(tmp_path):
    pytest.importorskip("reportlab")
    _gen().make_set(tmp_path / "set", count=3, variants=("pdf",))
    ts = bm.load_set(tmp_path / "set")
    assert len(ts.docs) == 3 and ts.schema.id == "supplier-invoice" and ts.docs[0].tags == ["pdf"]
    report = bm.run(ts, ["pdf-text", "no-such-engine"], _service())
    o = report["summary"]["pdf-text"]["overall"]
    assert o["errors"] == 0 and o["field_accuracy"] == 1.0 and o["char_accuracy"] > 0.99
    assert o["cloud_share"] == 0.0 and o["silent_errors"] == 0.0
    assert "no-such-engine" in report["skipped"]
    md = bm.write_report(report, tmp_path / "out", bm.TARGETS["pilot"], "pilot").read_text(encoding="utf-8")
    assert "| Field accuracy | ≥ 85.0% | 100.0% ✅ |" in md and "Skipped `no-such-engine`" in md
    assert json.loads((tmp_path / "out" / "results.json").read_text(encoding="utf-8"))["documents"] == 3


def test_cli_writes_report(tmp_path, capsys):
    pytest.importorskip("reportlab")
    _gen().make_set(tmp_path / "set", count=1, variants=("pdf",))
    rc = bm.main([str(tmp_path / "set"), "--engines", "pdf-text", "--out", str(tmp_path / "out"), "--no-local-env"])
    assert rc == 0 and (tmp_path / "out" / "report.md").exists()
    assert "Field accuracy" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_tesseract_reads_clean_synthetic_invoice(tmp_path):
    _gen().make_set(tmp_path / "set", count=1, variants=("clean",))
    row = bm.run(bm.load_set(tmp_path / "set"), ["tesseract"], _service())["rows"][0]
    assert row["error"] is None and row["char_accuracy"] > 0.9
    assert row["fields"]["subtotal"]["ok"] and row["fields"]["total"]["ok"]
