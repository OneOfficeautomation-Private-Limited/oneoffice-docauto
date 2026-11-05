"""PII masking before text goes to a cloud AI model (Claude, llm-cloud).

Saudi national ID / Iqama numbers, IBANs, phone numbers and e-mail addresses in the OCR text are replaced by
placeholders ([ID_1], [IBAN_1], [PHONE_1], [EMAIL_1]) before the request; the map stays in memory, and the values
the model returns are restored (a field read as "[IBAN_1]" becomes the IBAN again).

What it does not cover: page images. A picture of the page cannot be masked, so while masking is on, cloud models get
no page images unless the process allows them (EngineChoice.allowImages / ProcessOptions.cloudImages). Local engines
(llm-local, builtin-rules) are never masked: nothing leaves the machine.
"""
from __future__ import annotations

import re
from typing import Any

from .models import DocumentResult, ExtractedField

_DIGIT = "0-9٠-٩"          # Western and Arabic-Indic digits (OCR of Arabic documents gives both)
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    # IBAN: 2 letters, 2 check digits, 11-30 more (spaces allowed every 4); kept when the checksum holds or SA
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b")),
    # Saudi national ID (1...) and Iqama (2...): 10 digits
    ("ID", re.compile(rf"(?<![{_DIGIT}])[12١٢][{_DIGIT}]{{9}}(?![{_DIGIT}])")),
    # international (+966 / 00966 / +44 ...) and Saudi mobile / landline numbers
    ("PHONE", re.compile(r"(?<![\w+])(?:\+|00)\d{1,3}[ -]?\(?\d{1,4}\)?(?:[ -]?\d{2,4}){2,4}(?!\d)"
                         r"|(?<!\d)0(?:5\d|1\d)[ -]?\d{3}[ -]?\d{4}(?!\d)")),
]
TOKEN = re.compile(r"\[?\b(EMAIL|IBAN|ID|PHONE)_(\d+)\b\]?")


def _iban_ok(s: str) -> bool:
    s = s.replace(" ", "")
    if s.startswith("SA"):
        return len(s) == 24
    rearranged = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1
    except ValueError:
        return False


class PiiMasker:
    """Masks text consistently (the same value always gets the same placeholder) and restores values."""

    def __init__(self):
        self.values: dict[str, str] = {}      # placeholder -> original
        self._tokens: dict[str, str] = {}     # original -> placeholder
        self._count: dict[str, int] = {}

    def _token(self, kind: str, value: str) -> str:
        tok = self._tokens.get(value)
        if tok is None:
            self._count[kind] = self._count.get(kind, 0) + 1
            tok = f"[{kind}_{self._count[kind]}]"
            self._tokens[value], self.values[tok] = tok, value
        return tok

    def mask(self, text: str) -> str:
        if not text:
            return text
        for kind, pat in PATTERNS:
            def sub(m: re.Match, kind=kind) -> str:
                v = m.group(0)
                if kind == "IBAN" and not _iban_ok(v):
                    return v
                return self._token(kind, v)
            text = pat.sub(sub, text)
        return text

    @property
    def masked(self) -> int:
        return len(self.values)

    def unmask(self, value: Any) -> Any:
        if isinstance(value, str):
            return TOKEN.sub(lambda m: self.values.get(f"[{m.group(1)}_{m.group(2)}]", m.group(0)), value)
        if isinstance(value, list):
            return [self.unmask(v) for v in value]
        if isinstance(value, dict):
            return {k: self.unmask(v) for k, v in value.items()}
        return value

    def mask_document(self, doc: DocumentResult) -> DocumentResult:
        """A copy of the document with every text masked (same pages, lines and boxes, so citations still work)."""
        out = doc.model_copy(deep=True)
        for page in out.pages:
            for line in page.lines:
                line.text = self.mask(line.text)
            for w in page.words:
                w.text = self.mask(w.text)
            for b in page.blocks:
                b.text = self.mask(b.text)
            for t in page.tables:
                for c in t.cells:
                    c.text = self.mask(c.text)
            for kv in page.keyValuePairs:
                kv.key, kv.value = self.mask(kv.key), self.mask(kv.value)
            for bc in page.barcodes:
                bc.text = self.mask(bc.text)
        out.raw = None
        out.text = ""
        out.rebuild_text()
        return out

    def unmask_fields(self, fields: list[ExtractedField]) -> list[ExtractedField]:
        if not self.values:
            return fields
        from .extraction import coerce
        for f in fields:
            new = self.unmask(f.value)
            if new != f.value and isinstance(new, str) and f.type not in ("string", "enum", "table", "object"):
                typed, ok = coerce(new, f.type)
                new = typed if ok else new
            f.value = new
            if f.rawText:
                f.rawText = self.unmask(f.rawText)
        return fields
