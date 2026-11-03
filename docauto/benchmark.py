"""Accuracy benchmark: run a labelled test set through OCR engines and score the results.

Measures the success metrics of the OCR strategy plan per engine and per document tag:
character accuracy, field accuracy, straight-through rate, cloud share and turnaround, plus
how many documents were auto-approved with a wrong field (the "wrong amounts in exported data"
risk). Results are compared with the plan's pilot or cloud targets.

Test set: a folder of documents, each with a sibling "<name>.truth.json":

    {"fields": {"invoice_number": "INV-2026-0042", "total": 11500.0, ...},
     "text": "optional full text, used for character accuracy",
     "tags": ["scan"]}

An optional set.json gives {"name", "schema" (path, relative to the set), "languages"}.
Without a schema the benchmark uses samples/invoice_schema.json.

    python samples/make_benchmark_set.py                       # synthetic set (or use real documents)
    python -m docauto.benchmark benchmark-data/synthetic --engines tesseract,auto

Real client documents belong in benchmark-data/ (git-ignored), never in the repository.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .errors import DocAutoError
from .extraction import coerce
from .models import DocumentResult, FieldSchema

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA = ROOT / "samples" / "invoice_schema.json"
DOC_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
RETRY_THRESHOLD = 0.85   # the plan retries a page below this OCR confidence with the next engine

# (target, "min" = at least / "max" = at most). Proposals from the strategy plan, to confirm after the pilot.
TARGETS: dict[str, dict[str, tuple[float, str]]] = {
    "pilot": {"char_accuracy": (0.95, "min"), "field_accuracy": (0.85, "min"), "straight_through": (0.40, "min"),
              "cloud_share": (0.0, "max"), "turnaround_p95_s": (120.0, "max")},
    "cloud": {"char_accuracy": (0.98, "min"), "field_accuracy": (0.95, "min"), "straight_through": (0.70, "min"),
              "cloud_share": (0.20, "max"), "turnaround_p95_s": (60.0, "max")},
}


# ------------------------------------------------------------------ metrics
def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _squash(s: str) -> str:
    """Layout-neutral text: one space between words, no page breaks."""
    return re.sub(r"\s+", " ", s or "").strip()


def char_accuracy(ocr_text: str, truth_text: str) -> Optional[float]:
    truth = _squash(truth_text)
    if not truth:
        return None
    return max(0.0, 1.0 - levenshtein(_squash(ocr_text), truth) / len(truth))


def field_correct(ftype: str, got: Any, want: Any) -> bool:
    if want in (None, "", []):
        return got in (None, "", [])
    if got in (None, "", []):
        return False
    if ftype in ("number", "currency"):
        g, ok_g = (float(got), True) if isinstance(got, (int, float)) else coerce(str(got), ftype)
        w, ok_w = (float(want), True) if isinstance(want, (int, float)) else coerce(str(want), ftype)
        return ok_g and ok_w and abs(g - w) < 0.005
    if ftype == "date":
        g, ok_g = coerce(str(got), "date")
        w, ok_w = coerce(str(want), "date")
        return ok_g and ok_w and g == w
    if ftype == "boolean":
        return bool(got) == bool(want)
    return _squash(str(got)).casefold() == _squash(str(want)).casefold()


def p95(values: list[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    return s[max(0, -(-len(s) * 95 // 100) - 1)]   # nearest rank


# ------------------------------------------------------------------ test set
@dataclass
class TestDoc:
    path: Path
    fields: dict[str, Any]
    text: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class TestSet:
    name: str
    path: Path
    docs: list[TestDoc]
    schema: FieldSchema
    languages: list[str]


def load_set(path: str | Path, schema_path: str | Path | None = None,
             languages: Optional[list[str]] = None) -> TestSet:
    root = Path(path)
    if not root.is_dir():
        raise SystemExit(f"test set folder not found: {root}")
    meta = json.loads((root / "set.json").read_text(encoding="utf-8")) if (root / "set.json").exists() else {}
    sp = Path(schema_path) if schema_path else (root / meta["schema"] if meta.get("schema") else DEFAULT_SCHEMA)
    schema = FieldSchema.model_validate_json(sp.read_text(encoding="utf-8"))
    docs = []
    for p in sorted(root.iterdir()):
        if p.suffix.lower() not in DOC_SUFFIXES:
            continue
        tp = p.with_suffix(".truth.json")
        if not tp.exists():
            print(f"  skipped {p.name}: no {tp.name}", file=sys.stderr)
            continue
        t = json.loads(tp.read_text(encoding="utf-8"))
        docs.append(TestDoc(p, t.get("fields", {}), t.get("text", ""), list(t.get("tags", []))))
    if not docs:
        raise SystemExit(f"no labelled documents in {root} (each needs a <name>.truth.json)")
    return TestSet(meta.get("name", root.name), root, docs, schema, languages or meta.get("languages") or ["en"])


# ------------------------------------------------------------------ run
def _score_doc(svc, ts: TestSet, doc: TestDoc, result: DocumentResult, seconds: float) -> dict:
    types = {f.name: f.type for f in ts.schema.fields}
    got = {f.name: f.value for f in result.fields}
    fields = {name: {"want": want, "got": got.get(name),
                     "ok": field_correct(types.get(name, "string"), got.get(name), want)}
              for name, want in doc.fields.items()}
    page_confs = []
    for pg in result.pages:
        vals = [l.confidence for l in pg.lines if l.confidence is not None]
        page_confs.append(sum(vals) / len(vals) if vals else None)
    engines = [s.providerId for s in result.provenance.steps if s.stage == "digitize"]
    pages = len(result.pages)
    # pages sent to an outbound engine: per-page retries count their own pages; extraction sends the document
    cloud = 0
    for s in result.provenance.steps:
        p = svc.registry.get(s.providerId)
        if p is not None and not p.info.runsLocally:
            cloud = max(cloud, s.pages if s.stage == "digitize" else pages)
    all_ok = all(f["ok"] for f in fields.values())
    review = bool(result.review.required)
    return {
        "seconds": round(seconds, 3), "pages": pages, "engines": engines, "error": None,
        "char_accuracy": char_accuracy(result.text, doc.text),
        "ocr_confidence": result.mean_line_confidence(),
        "low_confidence_pages": sum(1 for c in page_confs if c is not None and c < RETRY_THRESHOLD),
        "fields": fields, "review_required": review,
        "review_reasons": [w for w in result.warnings if "review" in w or "failed" in w or "confidence" in w],
        "straight_through": all_ok and not review, "silent_error": not review and not all_ok,
        "cloud_pages": min(cloud, pages),
        "credits": round(sum(s.credits for s in result.provenance.steps), 4),
    }


def run(ts: TestSet, engines: list[str], service=None, extraction: str = "auto",
        progress: bool = False) -> dict:
    """Run every document through every engine; returns {"rows": [...], "summary": {...}}."""
    from .pipeline import DocumentService, ProcessOptions

    svc = service or DocumentService(_bench_config())
    rows, skipped = [], {}
    for engine in engines:
        if engine != "auto":
            p = svc.registry.get(engine)
            if p is None or not p.available:
                skipped[engine] = (p.reason if p else "unknown engine") or "not available"
                continue
        for i, doc in enumerate(ts.docs, 1):
            opt = ProcessOptions(ocrProvider=engine, strictProvider=engine != "auto", extractionProvider=extraction,
                                 languages=ts.languages, schema_=ts.schema, tenantId="benchmark")
            row = {"document": doc.path.name, "engine": engine, "tags": doc.tags}
            t0 = time.perf_counter()
            try:
                result = svc.process_bytes(doc.path.read_bytes(), doc.path.name, opt)
                row.update(_score_doc(svc, ts, doc, result, time.perf_counter() - t0))
            except DocAutoError as e:
                row.update({"seconds": round(time.perf_counter() - t0, 3), "error": e.message, "pages": 0,
                            "fields": {n: {"want": w, "got": None, "ok": False} for n, w in doc.fields.items()},
                            "straight_through": False, "silent_error": False, "cloud_pages": 0})
            rows.append(row)
            if progress:
                status = row["error"] or ("ok" if row["straight_through"] else
                                          "review" if row.get("review_required") else "WRONG, auto-approved")
                print(f"  [{engine}] {i}/{len(ts.docs)} {doc.path.name}: {status} ({row['seconds']:.1f}s)",
                      file=sys.stderr)
    return {"set": ts.name, "schema": ts.schema.id or ts.schema.name, "languages": ts.languages,
            "documents": len(ts.docs), "engines": engines, "skipped": skipped,
            "date": datetime.now().isoformat(timespec="seconds"), "rows": rows,
            "summary": {e: summarize([r for r in rows if r["engine"] == e]) for e in engines if e not in skipped}}


def summarize(rows: list[dict]) -> dict:
    """Aggregate metrics for one engine: overall, per tag and per field."""
    def agg(rs: list[dict]) -> dict:
        n = len(rs)
        fields = [f for r in rs for f in r["fields"].values()]
        chars = [r["char_accuracy"] for r in rs if r.get("char_accuracy") is not None]
        confs = [r["ocr_confidence"] for r in rs if r.get("ocr_confidence") is not None]
        pages = sum(r["pages"] for r in rs)
        return {
            "documents": n, "pages": pages, "errors": sum(1 for r in rs if r.get("error")),
            "char_accuracy": sum(chars) / len(chars) if chars else None,
            "field_accuracy": sum(f["ok"] for f in fields) / len(fields) if fields else None,
            "straight_through": sum(r["straight_through"] for r in rs) / n if n else None,
            "silent_errors": sum(r["silent_error"] for r in rs) / n if n else None,
            "review_rate": sum(1 for r in rs if r.get("review_required")) / n if n else None,
            "cloud_share": sum(r["cloud_pages"] for r in rs) / pages if pages else 0.0,
            "turnaround_p95_s": p95([r["seconds"] for r in rs if not r.get("error")]),
            "ocr_confidence": sum(confs) / len(confs) if confs else None,
            "low_confidence_pages": (sum(r.get("low_confidence_pages", 0) for r in rs) / pages) if pages else None,
            "credits": round(sum(r.get("credits", 0) for r in rs), 4),
        }

    tags = sorted({t for r in rows for t in r["tags"]})
    names = list(dict.fromkeys(n for r in rows for n in r["fields"]))
    return {
        "overall": agg(rows),
        "by_tag": {t: agg([r for r in rows if t in r["tags"]]) for t in tags},
        "by_field": {n: sum(r["fields"][n]["ok"] for r in rows if n in r["fields"]) /
                     max(1, sum(1 for r in rows if n in r["fields"])) for n in names},
    }


# ------------------------------------------------------------------ report
LABELS = [("char_accuracy", "Character accuracy", "pct"), ("field_accuracy", "Field accuracy", "pct"),
          ("straight_through", "Straight-through rate", "pct"),
          ("silent_errors", "Auto-approved with a wrong field", "pct"), ("review_rate", "Sent to review", "pct"),
          ("cloud_share", "Cloud share (pages)", "pct"), ("turnaround_p95_s", "Turnaround p95", "s"),
          ("ocr_confidence", "Mean OCR confidence", "pct"),
          ("low_confidence_pages", f"Pages below {RETRY_THRESHOLD:.0%} confidence", "pct"),
          ("errors", "Failed documents", "n")]


def _fmt(v: Any, kind: str) -> str:
    if v is None:
        return "–"
    if kind == "pct":
        return f"{v:.1%}"
    if kind == "s":
        return f"{v:.1f} s"
    return str(v)


def markdown(report: dict, targets: dict[str, tuple[float, str]], target_name: str) -> str:
    engines = list(report["summary"])
    out = [f"# Benchmark: {report['set']}", "",
           f"{report['date']} · {report['documents']} documents · schema `{report['schema']}` · "
           f"languages {', '.join(report['languages'])} · targets: {target_name}", ""]
    for e, why in report["skipped"].items():
        out.append(f"> Skipped `{e}`: {why}")
    if report["skipped"]:
        out.append("")
    if not engines:
        return "\n".join(out + ["No engine could run."])

    def table(title: str, block: dict[str, dict], with_targets: bool) -> list[str]:
        head = "| Metric |" + (" Target |" if with_targets else "") + "".join(f" {e} |" for e in block) + "\n"
        head += "|---|" + ("---|" if with_targets else "") + "---|" * len(block)
        lines = [f"## {title}", "", head]
        for key, label, kind in LABELS:
            t = targets.get(key)
            cells = []
            for e, m in block.items():
                v = m.get(key)
                mark = ""
                if with_targets and t and v is not None:
                    ok = v >= t[0] if t[1] == "min" else v <= t[0]
                    mark = " ✅" if ok else " ❌"
                cells.append(_fmt(v, kind) + mark)
            tgt = (("≥ " if t[1] == "min" else "≤ ") + _fmt(t[0], kind)) if t else "–"
            lines.append(f"| {label} |" + (f" {tgt} |" if with_targets else "") + "".join(f" {c} |" for c in cells))
        return lines + [""]

    out += table("Summary", {e: report["summary"][e]["overall"] for e in engines}, True)
    tags = sorted({t for e in engines for t in report["summary"][e]["by_tag"]})
    for t in tags:
        out += table(f"Document type: {t}", {e: report["summary"][e]["by_tag"].get(t, {}) for e in engines}, True)
    fields = list(dict.fromkeys(f for e in engines for f in report["summary"][e]["by_field"]))
    out += ["## Field accuracy", "", "| Field |" + "".join(f" {e} |" for e in engines),
            "|---|" + "---|" * len(engines)]
    for f in fields:
        out.append(f"| `{f}` |" + "".join(f" {_fmt(report['summary'][e]['by_field'].get(f), 'pct')} |"
                                          for e in engines))
    out += ["", "## Documents to look at", "",
            "Auto-approved with a wrong field first (the costly case), then failures and review.", ""]
    rank = lambda r: (0 if r["silent_error"] else 1 if r.get("error") else 2 if r.get("review_required") else 3)
    shown = 0
    for r in sorted(report["rows"], key=rank):
        if rank(r) == 3 or shown >= 40:
            break
        shown += 1
        what = ("auto-approved with errors" if r["silent_error"] else f"failed: {r['error']}" if r.get("error")
                else "sent to review")
        wrong = [f"`{n}` {v['got']!r} (want {v['want']!r})" for n, v in r["fields"].items() if not v["ok"]]
        out.append(f"- **{r['document']}** · {r['engine']} · {what}" + (": " + "; ".join(wrong) if wrong else ""))
    if not shown:
        out.append("None: every document went straight through.")
    return "\n".join(out) + "\n"


def write_report(report: dict, out_dir: Path, targets: dict[str, tuple[float, str]], target_name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md = out_dir / "report.md"
    md.write_text(markdown(report, targets, target_name), encoding="utf-8")
    return md


# ------------------------------------------------------------------ CLI
def _bench_config(config_path: Optional[str] = None) -> dict:
    from .config import load_config

    cfg = load_config(config_path or os.environ.get("DOCAUTO_CONFIG") or ROOT / "config" / "document-service.yaml")
    cfg["storage"]["data_dir"] = tempfile.mkdtemp(prefix="docauto-bench-")   # never touch the real database
    return cfg


def _load_local_env() -> None:
    """Pick up config/local.env like windows/start.ps1 does (Tesseract path, engine keys)."""
    from .settings import read_env

    for k, v in read_env().items():
        if v and not os.environ.get(k):
            os.environ[k] = v
    cmd = os.environ.get("TESSERACT_CMD")
    if cmd and Path(cmd).exists():
        os.environ["PATH"] = str(Path(cmd).parent) + os.pathsep + os.environ.get("PATH", "")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m docauto.benchmark", description=__doc__.splitlines()[0])
    ap.add_argument("set", help="test set folder (documents + <name>.truth.json)")
    ap.add_argument("--engines", default="tesseract", help="comma list of engine ids, or 'auto' for the router")
    ap.add_argument("--extraction", default="auto", help="extraction engine (default: the configured one)")
    ap.add_argument("--schema", help="field schema JSON (default: set.json, else samples/invoice_schema.json)")
    ap.add_argument("--languages", help="comma list, e.g. en,ar (default: set.json, else en)")
    ap.add_argument("--targets", default="pilot", help="pilot | cloud | path to a JSON file {metric: [value, min|max]}")
    ap.add_argument("--config", help="service config (default: config/document-service.yaml)")
    ap.add_argument("--out", help="report folder (default: benchmark-data/reports/<set>-<time>)")
    ap.add_argument("--no-local-env", action="store_true", help="do not read config/local.env")
    a = ap.parse_args(argv)

    if not a.no_local_env:
        _load_local_env()
    logging.getLogger("docauto").setLevel(logging.ERROR)   # failures are counted in the report instead
    if a.targets in TARGETS:
        targets, tname = TARGETS[a.targets], a.targets
    else:
        targets = {k: (float(v[0]), v[1]) for k, v in json.loads(Path(a.targets).read_text(encoding="utf-8")).items()}
        tname = Path(a.targets).name
    ts = load_set(a.set, a.schema, a.languages.split(",") if a.languages else None)
    engines = [e.strip() for e in a.engines.split(",") if e.strip()]
    print(f"Benchmark '{ts.name}': {len(ts.docs)} documents x {len(engines)} engine(s)", file=sys.stderr)

    from .pipeline import DocumentService
    report = run(ts, engines, DocumentService(_bench_config(a.config)), a.extraction, progress=True)
    out = Path(a.out) if a.out else ROOT / "benchmark-data" / "reports" / (
        re.sub(r"[^\w.-]+", "-", Path(a.set).resolve().name) + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    md = write_report(report, out, targets, tname)

    print()
    for key, label, kind in LABELS[:7]:
        vals = "  ".join(f"{e}={_fmt(report['summary'][e]['overall'].get(key), kind)}" for e in report["summary"])
        print(f"{label:<34} {vals}")
    for e, why in report["skipped"].items():
        print(f"skipped {e}: {why}")
    print(f"\nReport: {md}")
    return 0 if report["summary"] else 1


if __name__ == "__main__":
    sys.exit(main())
