"""Approximate USD cost per 1,000 pages per engine, and a monthly estimate for a routing mix.

List prices come from the OCR strategy plan (Nov 2026) and Anthropic's per-token prices.
They are indicative: check each vendor's pricing page before quoting a client. Billing
inside DocFlow stays in credits (manifest creditCostPerPage); this is for planning.
"""
from __future__ import annotations

from typing import Optional

# engine id -> {"ocr": usd per 1,000 pages read, "extract": usd per 1,000 pages with field extraction}
LIST_PRICES: dict[str, dict[str, float]] = {
    "pdf-text": {"ocr": 0.0}, "tesseract": {"ocr": 0.0}, "paddleocr": {"ocr": 0.0},
    "builtin-rules": {"extract": 0.0}, "llm-local": {"extract": 0.0},
    "azure-di": {"ocr": 1.50, "extract": 10.0},        # Read; prebuilt invoice/receipt models
    "google-docai": {"ocr": 1.50},                     # Document OCR
    "aws-textract": {"ocr": 1.50, "extract": 10.0},    # DetectDocumentText; AnalyzeExpense
}
# Claude: tokens per page (page image ~1,600 + OCR text + prompt in; JSON reply + thinking out), estimate
CLAUDE_TOKENS_PER_PAGE = {"input": 3000, "output": 1000}
# input / output USD per million tokens. Haiku 5.5's rate is for prompts of 100K tokens or fewer (a page is ~3K);
# longer prompts cost $0.50 / $2.50.
CLAUDE_USD_PER_MTOK = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00),
                       "claude-haiku-5-5": (0.10, 0.50)}
DEFAULT_CLAUDE_MODEL = "claude-haiku-5-5"
DEFAULT_ESCALATION_MODEL = "claude-sonnet-5-5"


def claude_per_1000(model: str) -> Optional[float]:
    if model not in CLAUDE_USD_PER_MTOK:
        return None
    inp, out = CLAUDE_USD_PER_MTOK[model]
    t = CLAUDE_TOKENS_PER_PAGE
    return round(1000 * (t["input"] * inp + t["output"] * out) / 1e6, 2)


def price_per_1000(engine: str, use: str, model: Optional[str] = None) -> Optional[float]:
    """use: 'ocr' or 'extract'. None = no list price known (BYOK contract, template engine...)."""
    if engine == "claude":
        return claude_per_1000(model or DEFAULT_CLAUDE_MODEL) if use == "extract" else None
    return LIST_PRICES.get(engine, {}).get(use)


def _claude_config(config: dict) -> dict:
    return ((config.get("providers", {}).get("settings", {}).get("claude", {}) or {}).get("config", {}) or {})


def claude_model(config: dict) -> str:
    return _claude_config(config).get("model") or DEFAULT_CLAUDE_MODEL


def claude_escalation_model(config: dict) -> Optional[str]:
    """The model that re-reads documents below the review threshold (None = escalation off)."""
    return _claude_config(config).get("escalationModel", DEFAULT_ESCALATION_MODEL) or None


def price_table(config: dict, engine_ids: list[str]) -> dict[str, dict[str, Optional[float]]]:
    model = claude_model(config)
    return {e: {"ocr": price_per_1000(e, "ocr", model), "extract": price_per_1000(e, "extract", model)}
            for e in engine_ids}


def estimate(pages: int, cloud_share: float, extract_share: float, cloud_ocr: Optional[str],
             extraction: str, model: Optional[str] = None, cloud_opt_in: bool = True) -> dict:
    """Monthly service cost like the plan's example: local pages are free, weak pages go to cloud OCR,
    pages that need fields go to the extraction engine. Without cloud opt-in nothing outbound runs."""
    pages = max(0, int(pages))
    cloud_share = min(1.0, max(0.0, float(cloud_share)))
    extract_share = min(1.0, max(0.0, float(extract_share)))
    cloud_pages = round(pages * cloud_share) if cloud_ocr and cloud_opt_in else 0
    extract_pages = round(pages * extract_share)
    ocr_price = price_per_1000(cloud_ocr, "ocr", model) if cloud_ocr else 0.0
    ext_price = price_per_1000(extraction, "extract", model)
    outbound_extract = extraction not in ("builtin-rules", "llm-local")
    if outbound_extract and not cloud_opt_in:
        ext_price, note = 0.0, "cloud opt-in is off: fields use local rules"
    else:
        note = ""
    lines = [
        {"label": "Local OCR (Tesseract, PaddleOCR)", "pages": pages - cloud_pages, "per1000": 0.0, "usd": 0.0},
        {"label": f"Cloud OCR for weak pages ({cloud_ocr or 'none'})", "pages": cloud_pages, "per1000": ocr_price,
         "usd": None if ocr_price is None else round(cloud_pages * ocr_price / 1000, 2)},
        {"label": f"Field extraction ({extraction}{' ' + model if extraction == 'claude' and model else ''})",
         "pages": extract_pages, "per1000": ext_price,
         "usd": None if ext_price is None else round(extract_pages * ext_price / 1000, 2)},
    ]
    known = [l["usd"] for l in lines if l["usd"] is not None]
    total = round(sum(known), 2)
    return {"pages": pages, "lines": lines, "total": total, "complete": len(known) == len(lines),
            "perPage": round(total / pages, 5) if pages else 0.0, "note": note}
