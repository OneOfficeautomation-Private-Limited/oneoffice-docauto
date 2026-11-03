"""Field validation, confidence scoring and the human-review decision.

Order: per-field checks (a value that exactly matches its pattern is slightly
corroborated) -> cross-field rules (passing rules corroborate their fields) ->
per-field review decision -> document confidence.

The boosts are calibrated with the benchmark (python -m docauto.benchmark): local
OCR rarely rates a line above ~0.93, so without corroboration a correctly read
identifier (0.9 x 0.93 = 0.84) could never pass the 0.85 review threshold. A
pattern match is weaker evidence than an arithmetic rule, because a misread digit
can still fit the pattern, hence the smaller boost.
"""
from __future__ import annotations

import ast
import re

from .extraction import safe_eval
from .models import DocumentResult, FieldSchema, ReviewStatus

PATTERN_BOOST = 0.05   # value fully matches the field's regex
RULE_BOOST = 0.15      # every cross-field rule naming the field passed
MAX_CONFIDENCE = 0.99


def _rule_names(rule: str) -> set[str]:
    try:
        return {n.id for n in ast.walk(ast.parse(rule, mode="eval")) if isinstance(n, ast.Name)}
    except SyntaxError:
        return set()


def score(doc: DocumentResult, schema: FieldSchema | None, field_threshold: float,
          doc_threshold: float) -> DocumentResult:
    reasons: list[str] = []
    defs = {f.name: f for f in schema.fields} if schema else {}

    # 1) per-field checks
    for fld in doc.fields:
        fd = defs.get(fld.name)
        errs = list(fld.validation.errors)  # keep extractor-raised errors (e.g. hallucination guard)
        if fd:
            if fd.required and fld.value in (None, "", []):
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
            fld.confidence = round(min(MAX_CONFIDENCE, fld.confidence + PATTERN_BOOST), 4)

    # 2) cross-field rules
    if schema and schema.validation:
        values = {f.name: f.value for f in doc.fields}
        for rule in schema.validation:
            try:
                ok = bool(safe_eval(rule, values))
                label = rule
            except Exception as e:
                ok, label = False, f"{rule} ({type(e).__name__})"
            if not ok:
                reasons.append(f"validation rule failed: {label}")
                continue
            names = _rule_names(rule)
            for fld in doc.fields:  # a passing consistency rule corroborates its fields
                if fld.name in names and fld.confidence is not None and fld.validation.passed:
                    fld.confidence = round(min(MAX_CONFIDENCE, fld.confidence + RULE_BOOST), 4)

    # 3) per-field review decision
    for fld in doc.fields:
        fd = defs.get(fld.name)
        relevant = fd is None or fd.required or fld.value not in (None, "", [])
        if not relevant:
            continue
        threshold = fd.confidenceThreshold if fd and fd.confidenceThreshold is not None else field_threshold
        if not fld.validation.passed or fld.confidence is None or fld.confidence < threshold:
            reasons.append(f"field '{fld.name}' needs review")

    # 4) document confidence
    ocr_conf = doc.mean_line_confidence()
    field_confs = [f.confidence for f in doc.fields if f.confidence is not None
                   and (defs.get(f.name) is None or defs[f.name].required or f.value not in (None, "", []))]
    parts = ([ocr_conf] if ocr_conf is not None else []) + field_confs
    doc.confidence = round(sum(parts) / len(parts), 4) if parts else None
    if doc.confidence is not None and doc.confidence < doc_threshold:
        reasons.append(f"document confidence {doc.confidence} < {doc_threshold}")

    doc.review.required = bool(reasons)
    doc.review.status = ReviewStatus.pending if reasons else ReviewStatus.not_required
    doc.warnings.extend(r for r in reasons if r not in doc.warnings)
    return doc
