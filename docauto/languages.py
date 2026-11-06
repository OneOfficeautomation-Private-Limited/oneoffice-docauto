"""The languages documents can be read in.

Document types store short language codes ("en", "ar", ...). Each OCR engine names them its own way (Tesseract
"eng", "ara", "chi_sim"; PaddleOCR "en", "arabic", "ch"), so this table maps between them. Which languages an
engine can read on this server comes from the engine itself (Tesseract: its installed language data, as
`tesseract --list-langs` lists it; PaddleOCR: its supported set, models download on first use).

The administrator chooses the languages offered when people set up a document type (DOCAUTO_OFFERED_LANGUAGES,
default en,ar): a short list keeps the choice simple, and languages that are not installed are shown as such.
"""
from __future__ import annotations

from typing import Iterable, Optional

DEFAULT_OFFERED = ("en", "ar")

# code, name, Tesseract code, PaddleOCR code (None: that engine does not read it)
TABLE: list[tuple[str, str, Optional[str], Optional[str]]] = [
    ("en", "English", "eng", "en"), ("ar", "Arabic", "ara", "arabic"), ("ur", "Urdu", "urd", "ur"),
    ("fa", "Persian", "fas", "fa"), ("hi", "Hindi", "hin", "hi"), ("bn", "Bengali", "ben", None),
    ("ta", "Tamil", "tam", "ta"), ("te", "Telugu", "tel", "te"), ("ml", "Malayalam", "mal", None),
    ("mr", "Marathi", "mar", "mr"), ("ne", "Nepali", "nep", "ne"), ("pa", "Punjabi", "pan", None),
    ("gu", "Gujarati", "guj", None), ("kn", "Kannada", "kan", "ka"), ("ps", "Pashto", "pus", None),
    ("fr", "French", "fra", "fr"), ("de", "German", "deu", "german"), ("es", "Spanish", "spa", "es"),
    ("it", "Italian", "ita", "it"), ("pt", "Portuguese", "por", "pt"), ("nl", "Dutch", "nld", "nl"),
    ("pl", "Polish", "pol", "pl"), ("ru", "Russian", "rus", "ru"), ("uk", "Ukrainian", "ukr", "uk"),
    ("tr", "Turkish", "tur", "tr"), ("el", "Greek", "ell", None), ("he", "Hebrew", "heb", None),
    ("sv", "Swedish", "swe", "sv"), ("zh", "Chinese (simplified)", "chi_sim", "ch"),
    ("zh-Hant", "Chinese (traditional)", "chi_tra", "chinese_cht"), ("ja", "Japanese", "jpn", "japan"),
    ("ko", "Korean", "kor", "korean"), ("th", "Thai", "tha", None), ("vi", "Vietnamese", "vie", "vi"),
    ("id", "Indonesian", "ind", "id"), ("ms", "Malay", "msa", "ms"), ("tl", "Filipino", "tgl", None),
    ("sw", "Swahili", "swa", "sw"),
]
NAMES = {c: n for c, n, _, _ in TABLE}
TESSERACT = {c: t for c, _, t, _ in TABLE if t}
PADDLE = {c: p for c, _, _, p in TABLE if p}
_FROM_TESSERACT = {t: c for c, t in TESSERACT.items()}
# Tesseract data that is not a language to offer: orientation detection, equations, scripts ("script/Arabic")
_NOT_LANGUAGES = {"osd", "equ", "snum"}


def tesseract_code(code: str) -> str:
    """"ar" -> "ara"; a code not in the table is passed on as it is (it may be Tesseract's own)."""
    return TESSERACT.get(code, code)


def paddle_code(code: str) -> Optional[str]:
    return PADDLE.get(code)


def from_tesseract(tess: str) -> Optional[str]:
    """"ara" -> "ar"; Tesseract data that is not a language -> None; one not in the table keeps its own code."""
    if tess in _NOT_LANGUAGES or "/" in tess or not tess:
        return None
    return _FROM_TESSERACT.get(tess, tess)


def name(code: str) -> str:
    return NAMES.get(code, code)


def parse(value: object) -> list[str]:
    """A comma separated list (the setting) as codes, in order, without repeats; empty -> the default."""
    if isinstance(value, (list, tuple)):
        items: Iterable[str] = (str(v) for v in value)
    else:
        items = str(value or "").split(",")
    out = list(dict.fromkeys(i.strip() for i in items if i and i.strip()))
    return out or list(DEFAULT_OFFERED)


def offered(config: dict) -> list[str]:
    """The administrator's list (DOCAUTO_OFFERED_LANGUAGES, through config languages.offered)."""
    return parse((config.get("languages") or {}).get("offered"))


def available(registry) -> dict[str, list[str]]:
    """{language code: [ids of the OCR engines on this server that can read it]}. An engine says what it reads with
    `installed_languages()` (codes); an engine that cannot run reads nothing."""
    out: dict[str, list[str]] = {}
    for pid in ("tesseract", "paddleocr"):
        p = registry.get(pid)
        if not p or not p.available or p.instance is None or not hasattr(p.instance, "installed_languages"):
            continue
        try:
            codes = p.instance.installed_languages()
        except Exception:  # an engine that cannot say reads nothing here
            codes = []
        for c in codes:
            out.setdefault(c, []).append(pid)
    return out


def describe(config: dict, registry) -> list[dict]:
    """Every language the screens may show: the offered ones first (in the administrator's order), then the others
    the engines can read, then the rest of the table. installed: at least one engine here can read it."""
    have = available(registry)
    chosen = offered(config)
    rest = sorted((c for c in have if c not in chosen), key=name)
    others = sorted((c for c in NAMES if c not in chosen and c not in have), key=name)
    return [{"code": c, "name": name(c), "engines": have.get(c, []), "installed": c in have, "offered": c in chosen}
            for c in [*chosen, *rest, *others]]
