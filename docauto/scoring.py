"""Field validation, confidence scoring and the human-review decision.

Order: per-field checks (a value that exactly matches its pattern is slightly
corroborated) -> cross-field rules (passing rules corroborate their fields) ->
document confidence -> review decision. The decision is kept as structured reasons
(review_reasons(): which field, why, the threshold and confidence) so a review
screen can say what to check; review_reasons() alone re-states them for a stored
result without changing it.

The boosts are calibrated with the benchmark (python -m docauto.benchmark): local
OCR rarely rates a line above ~0.93, so without corroboration a correctly read
identifier (0.9 x 0.93 = 0.84) could never pass the 0.85 review threshold. A
pattern match is weaker evidence than an arithmetic rule, because a misread digit
can still fit the pattern, hence the smaller boost.
"""
from __future__ import annotations

import ast
import re
from typing import Optional

from .extraction import safe_eval
from .grounding import HALLUCINATION
from .models import DocumentResult, ExtractedField, FieldSchema, ReviewReason, ReviewStatus

PATTERN_BOOST = 0.05   # value fully matches the field's regex
RULE_BOOST = 0.15      # every cross-field rule naming the field passed
MAX_CONFIDENCE = 0.99


def _rule_names(rule: str) -> set[str]:
    try:
        return {n.id for n in ast.walk(ast.parse(rule, mode="eval")) if isinstance(n, ast.Name)}
    except SyntaxError:
        return set()


def field_threshold_for(name: str, fd, default: float, overrides: dict[str, float] | None = None) -> float:
    if overrides and overrides.get(name) is not None:
        return float(overrides[name])
    if fd is not None and fd.confidenceThreshold is not None:
        return fd.confidenceThreshold
    return default


def _pct(v: float) -> str:
    return f"{round(v * 100)}%"


def _empty(v) -> bool:
    return v in (None, "", [])


def _rule_label(rule: str, values: dict) -> Optional[str]:
    """None when the cross-field rule passes, else how it is reported."""
    try:
        return None if bool(safe_eval(rule, values)) else rule
    except Exception as e:
        return f"{rule} ({type(e).__name__})"


def _field_reasons(fld: ExtractedField, fd, threshold: float) -> list[ReviewReason]:
    """Why one field needs a person (empty: it does not). The checks and confidence are taken as they are."""
    if fd is not None and fd.required and _empty(fld.value):
        return [ReviewReason(field=fld.name, reason="required_missing", confidence=fld.confidence,
                             message="Required, but not found on the document")]
    out = []
    for e in fld.validation.errors:
        if e == HALLUCINATION:
            out.append(ReviewReason(field=fld.name, reason="not_grounded", confidence=fld.confidence,
                                    message="Not found on the page: the engine may have made this value up"))
        elif e != "required field missing":
            out.append(ReviewReason(field=fld.name, reason="validation", confidence=fld.confidence,
                                    message=e[:1].upper() + e[1:]))
    if not fld.validation.passed and not out:
        out.append(ReviewReason(field=fld.name, reason="validation", confidence=fld.confidence,
                                message="Failed a check"))
    if not out and (fld.confidence is None or fld.confidence < threshold):
        out.append(ReviewReason(
            field=fld.name, reason="low_confidence", threshold=threshold, confidence=fld.confidence,
            message=(f"Confidence {_pct(fld.confidence)} is under the {_pct(threshold)} needed"
                     if fld.confidence is not None else "The engine gave no confidence for this value")))
    return out


def review_reasons(doc: DocumentResult, schema: FieldSchema | None, field_threshold: float, doc_threshold: float,
                   field_thresholds: dict[str, float] | None = None,
                   failed_rules: list[str] | None = None) -> list[ReviewReason]:
    """Why the document needs review as it stands (the decision score() makes; it changes nothing): failed
    cross-field rules, then each field, then the document confidence. failed_rules: already evaluated by score()."""
    defs = {f.name: f for f in schema.fields} if schema else {}
    if failed_rules is None:
        values = {f.name: f.value for f in doc.fields}
        failed_rules = [l for r in (schema.validation if schema else []) if (l := _rule_label(r, values))]
    names = {f.name for f in doc.fields}
    out = [ReviewReason(reason="check_failed", message=f"Check failed: {label}",
                        fields=sorted(_rule_names(label.split(" (")[0]) & names)) for label in failed_rules]
    for fld in doc.fields:
        fd = defs.get(fld.name)
        if fd is None or fd.required or not _empty(fld.value):   # an optional field left empty is no reason
            out += _field_reasons(fld, fd, field_threshold_for(fld.name, fd, field_threshold, field_thresholds))
    if doc.confidence is not None and doc.confidence < doc_threshold:
        out.append(ReviewReason(reason="document_confidence", threshold=doc_threshold, confidence=doc.confidence,
                                message=f"Document confidence {_pct(doc.confidence)} is under the "
                                        f"{_pct(doc_threshold)} needed"))
    return out


def _warning(r: ReviewReason) -> str:
    if r.reason == "check_failed":
        return "validation rule failed: " + r.message.removeprefix("Check failed: ")
    if r.reason == "document_confidence":
        return f"document confidence {r.confidence} < {r.threshold}"
    return f"field '{r.field}' needs review"


def score(doc: DocumentResult, schema: FieldSchema | None, field_threshold: float,
          doc_threshold: float, field_thresholds: dict[str, float] | None = None) -> DocumentResult:
    """field_thresholds: a process's own threshold per field name; it wins over the document type's
    confidenceThreshold, which wins over field_threshold (the process-wide value). The reasons for review are
    kept in doc.review.reasons (structured) and doc.warnings (one line each)."""
    defs = {f.name: f for f in schema.fields} if schema else {}

    # 1) per-field checks
    for fld in doc.fields:
        fd = defs.get(fld.name)
        errs = list(fld.validation.errors)  # keep extractor-raised errors (e.g. hallucination guard)
        if fd:
            if fd.required and _empty(fld.value):
                errs.append("required field missing")
            if fd.type == "enum" and fd.enumValues and fld.value not in (None, "") and fld.value not in fd.enumValues:
                errs.append(f"value not in {fd.enumValues}")
            if (fd.regex and isinstance(fld.value, str) and fld.value and not re.search(fd.regex, fld.value)
                    and not (fld.rawText and re.search(fd.regex, fld.rawText))):
                errs.append("does not match pattern")
        new_errs = [e for e in errs if e not in fld.validation.errors]
        fld.validation.errors = errs
        fld.validation.passed = not errs
        if new_errs and fld.confidence:
            fld.confidence = round(fld.confidence * 0.5, 4)
        elif (fd and fd.regex and not errs and fld.confidence and isinstance(fld.value, str)
              and re.fullmatch(fd.regex, fld.value)):
            fld.confidence = round(max(fld.confidence, min(MAX_CONFIDENCE, fld.confidence + PATTERN_BOOST)), 4)

    # 2) cross-field rules
    failed: list[str] = []
    if schema and schema.validation:
        values = {f.name: f.value for f in doc.fields}
        for rule in schema.validation:
            label = _rule_label(rule, values)
            if label is not None:
                failed.append(label)
                continue
            names = _rule_names(rule)
            for fld in doc.fields:  # a passing consistency rule corroborates its fields
                if fld.name in names and fld.confidence is not None and fld.validation.passed:
                    fld.confidence = round(max(fld.confidence, min(MAX_CONFIDENCE, fld.confidence + RULE_BOOST)), 4)

    # 3) document confidence
    ocr_conf = doc.mean_line_confidence()
    field_confs = [f.confidence for f in doc.fields if f.confidence is not None
                   and (defs.get(f.name) is None or defs[f.name].required or not _empty(f.value))]
    parts = ([ocr_conf] if ocr_conf is not None else []) + field_confs
    doc.confidence = round(sum(parts) / len(parts), 4) if parts else None

    # 4) review decision
    reasons = review_reasons(doc, schema, field_threshold, doc_threshold, field_thresholds, failed)
    doc.review.reasons = reasons
    doc.review.required = bool(reasons)
    doc.review.status = ReviewStatus.pending if reasons else ReviewStatus.not_required
    lines = list(dict.fromkeys(_warning(r) for r in reasons))
    doc.warnings.extend(w for w in lines if w not in doc.warnings)
    return doc
