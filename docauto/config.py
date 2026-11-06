"""Service configuration (YAML) with ${ENV_VAR} substitution for secrets."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")

DEFAULT_CONFIG: dict[str, Any] = {
    "deployment_profile": "onprem_airgapped",
    "option_packs": {"tier1_opensource": True, "tier2_cloud": False, "tier3_ai": False},
    "residency": {"allowed_regions": [], "allow_cross_border": False},
    "providers": {"allow": ["*"], "deny": [], "settings": {}},
    "defaults": {"screen_ocr": "tesseract", "document_ocr": "tesseract", "extraction": "builtin-rules"},
    "fallback": {"on_error": ["tesseract"], "low_confidence": {"threshold": 0.0, "escalate_to": None}},
    "human_review": {"field_confidence_threshold": 0.85, "document_confidence_threshold": 0.80},
    # database_url: None = DOCAUTO_DATABASE_URL, else an embedded PostgreSQL in <data_dir>/postgres (docauto.db)
    "storage": {"data_dir": "./data", "retention_days": 30, "database_url": None, "schema_prefix": None,
                "key_file": None, "tenant": "default"},
    "privacy": {"mask_pii": True},
    "languages": {"offered": "en,ar"},   # offered for document types (docauto.languages)
    "options_dir": None,
}


def _subst(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        out = _ENV.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), value)
        if out != value:  # typed values coming from environment variables
            low = out.strip().lower()
            if low in ("true", "yes", "on"):
                return True
            if low in ("false", "no", "off"):
                return False
            if low in ("", "null", "none"):
                return None
        return out
    if isinstance(value, dict):
        return {k: _subst(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, env) for v in value]
    return value


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None, env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """env: the variables to substitute (default: the process environment; a tenant adds its own settings)."""
    path = path or os.environ.get("DOCAUTO_CONFIG")
    cfg = DEFAULT_CONFIG
    if path and Path(path).exists():
        with open(path, encoding="utf-8") as f:
            cfg = _merge(DEFAULT_CONFIG, yaml.safe_load(f) or {})
    cfg = _subst(cfg, os.environ if env is None else env)
    if not cfg.get("options_dir"):
        cfg["options_dir"] = str(Path(__file__).resolve().parent.parent / "options")
    return cfg
