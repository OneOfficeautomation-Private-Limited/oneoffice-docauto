"""Editable settings (config/local.env) exposed to the web UI.

The YAML config reads ${ENV_VAR}s; this module defines which variables the UI
may edit, how they are grouped (by option pack / engine), and writes them back
to local.env while preserving comments. Secrets are never returned in clear.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

MASK = "••••••••"

# field: key, label, type (text|secret|bool|select|number|path), help, options, default
DEPLOYMENT = [
    {"key": "DOCAUTO_PROFILE", "label": "Deployment profile", "type": "select",
     "options": ["onprem_airgapped", "onprem_hybrid", "cloud_saas"], "default": "onprem_hybrid",
     "help": "Air-gapped: only local engines. Hybrid: local + approved cloud engines. SaaS: everything."},
    {"key": "DOCAUTO_ALLOWED_REGIONS", "label": "Allowed processing regions", "type": "text", "default": "",
     "help": "Comma-separated (e.g. uaenorth, me-central-1). Empty = no region restriction."},
    {"key": "DOCAUTO_DENY", "label": "Disabled engines", "type": "text", "default": "",
     "help": "Comma-separated engine ids that must never be used."},
    {"key": "DOCAUTO_API_TOKEN", "label": "API token for the RPA tool", "type": "secret", "default": "",
     "help": "Empty = no authentication (local testing only)."},
    {"key": "DOCAUTO_PLATFORM_URL", "label": "Command Center address", "type": "text", "default": "",
     "placeholder": "http://127.0.0.1:8720",
     "help": "Where \"Start a bot\" deliveries start their bot. In SaaS, {tenant} is replaced by the tenant."},
    {"key": "DOCAUTO_RETENTION_DAYS", "label": "Keep documents for (days)", "type": "number", "default": "30",
     "help": "Documents (files, results) older than this are deleted every day. 0 = keep forever. Documents still "
             "waiting for review and test-set documents are kept."},
]
DEFAULTS = [
    {"key": "DOCAUTO_DEFAULT_SCREEN_OCR", "label": "Screen OCR engine", "type": "engine", "kind": "ocr", "default": "tesseract"},
    {"key": "DOCAUTO_DEFAULT_OCR", "label": "Document OCR engine", "type": "engine", "kind": "ocr", "default": "tesseract"},
    {"key": "DOCAUTO_DEFAULT_EXTRACTION", "label": "Extraction engine", "type": "engine", "kind": "extraction",
     "default": "builtin-rules"},
    {"key": "DOCAUTO_CLOUD_OPT_IN", "label": "Cloud opt-in", "type": "bool", "default": "false",
     "help": "Off = local-only: no engine that sends documents off this machine may run (OneConnect, cloud OneMind)."},
    {"key": "DOCAUTO_ESCALATE_THRESHOLD", "label": "Retry pages below this OCR confidence (0-1)", "type": "number",
     "default": "0.85"},
    {"key": "DOCAUTO_RETRY_LOCAL", "label": "Local engines that re-read weak pages", "type": "text",
     "default": "paddleocr", "help": "Comma-separated engine ids, tried in order. Free."},
    {"key": "DOCAUTO_ESCALATE_TO", "label": "Cloud engine for pages still weak", "type": "engine", "kind": "ocr",
     "default": "", "allowEmpty": True, "help": "Only with cloud opt-in. Costs credits per page."},
    {"key": "DOCAUTO_FIELD_THRESHOLD", "label": "Field review threshold (0-1)", "type": "number", "default": "0.85",
     "help": "Fields below this confidence go to the Validation Station."},
    {"key": "DOCAUTO_DOC_THRESHOLD", "label": "Document review threshold (0-1)", "type": "number", "default": "0.80"},
    {"key": "DOCAUTO_MASK_PII", "label": "Mask personal data before cloud AI", "type": "bool", "default": "true",
     "help": "National ID / Iqama numbers, IBANs, phone numbers and e-mails are replaced by placeholders before text "
             "goes to a cloud AI model, and restored in the results. Page images cannot be masked: with this on they "
             "are only sent when a process allows images. Local engines are never masked."},
    {"key": "DOCAUTO_OFFERED_LANGUAGES", "label": "Languages offered for document types", "type": "text",
     "default": "en,ar", "help": "Language codes, comma separated (en, ar, ur, hi, fr, zh...). People choose a "
                                 "document type's languages from these; GET /df/languages lists what the OCR "
                                 "engines here can read."},
]
PACKS = {
    "tier1_opensource": {"key": "DOCAUTO_PACK_OPENSOURCE", "title": "OneCore · Open source",
                         "summary": "Free engines that run on this machine. Nothing leaves the network."},
    "tier2_cloud": {"key": "DOCAUTO_PACK_CLOUD", "title": "OneConnect · Cloud & licensed",
                    "summary": "Commercial OCR/IDP services. Bring your own key, or use keys resold by Oneoffice."},
    "tier3_ai": {"key": "DOCAUTO_PACK_AI", "title": "OneMind · AI extraction",
                 "summary": "Large language models read any document against your field list. No templates."},
}
ENGINES: dict[str, dict[str, Any]] = {
    "tesseract": {"fields": [
        {"key": "TESSERACT_CMD", "label": "tesseract.exe path", "type": "path", "default": "",
         "help": "Blank = find on PATH / default install folder."},
        {"key": "TESSDATA_PREFIX", "label": "Language data folder (tessdata)", "type": "path", "default": ""}],
        "install": None},
    "paddleocr": {"fields": [], "install": "pip install paddleocr paddlepaddle"},
    "azure-di": {"fields": [
        {"key": "AZURE_DI_ENDPOINT", "label": "Endpoint", "type": "text", "default": "",
         "placeholder": "https://<resource>.cognitiveservices.azure.com"},
        {"key": "AZURE_DI_KEY", "label": "Key", "type": "secret", "default": ""},
        {"key": "AZURE_DI_REGION", "label": "Region", "type": "text", "default": "westeurope",
         "help": "Used by the residency policy, e.g. uaenorth."}], "install": None},
    "google-docai": {"fields": [
        {"key": "GCP_PROJECT", "label": "GCP project id", "type": "text", "default": ""},
        {"key": "DOCAI_LOCATION", "label": "Location", "type": "select", "options": ["us", "eu"], "default": "eu"},
        {"key": "DOCAI_OCR_PROCESSOR", "label": "Document OCR processor id", "type": "text", "default": ""},
        {"key": "DOCAI_FORM_PROCESSOR", "label": "Form Parser processor id", "type": "text", "default": ""},
        {"key": "GOOGLE_APPLICATION_CREDENTIALS", "label": "Service-account JSON path", "type": "path", "default": ""}],
        "install": "pip install google-cloud-documentai"},
    "aws-textract": {"fields": [
        {"key": "AWS_REGION", "label": "Region", "type": "text", "default": "", "placeholder": "me-central-1"},
        {"key": "AWS_ACCESS_KEY_ID", "label": "Access key id", "type": "text", "default": ""},
        {"key": "AWS_SECRET_ACCESS_KEY", "label": "Secret access key", "type": "secret", "default": ""}],
        "install": "pip install boto3"},
    "abbyy": {"fields": [
        {"key": "ABBYY_PRODUCT", "label": "ABBYY product", "type": "select", "options": ["", "vantage", "fre", "cloud"],
         "default": "", "help": "Template connector: needs implementation for the product in your ABBYY agreement."}],
        "install": None},
    "llm-cloud": {"fields": [
        {"key": "LLM_BASE_URL", "label": "API base URL", "type": "text", "default": "",
         "placeholder": "https://<provider>/v1", "help": "Any OpenAI-compatible /chat/completions endpoint."},
        {"key": "LLM_MODEL", "label": "Model id", "type": "text", "default": ""},
        {"key": "LLM_API_KEY", "label": "API key", "type": "secret", "default": ""},
        {"key": "LLM_REGION", "label": "Processing region", "type": "text", "default": ""},
        {"key": "LLM_SEND_IMAGES", "label": "Send page images (vision models)", "type": "bool", "default": "true"}],
        "install": None},
    "claude": {"fields": [
        {"key": "CLAUDE_API_KEY", "label": "Anthropic API key", "type": "secret", "default": ""},
        {"key": "CLAUDE_MODEL", "label": "Model", "type": "select",
         "options": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"], "default": "claude-haiku-5-5",
         "help": "Haiku 5.5: cheapest, the default. Sonnet 5.5: mid-tier. Opus 5.5: most accurate, most expensive."},
        {"key": "CLAUDE_ESCALATION_MODEL", "label": "Escalation model", "type": "select",
         "options": ["claude-sonnet-5-5", "claude-opus-5-5", "none"], "default": "claude-sonnet-5-5",
         "help": "Re-reads a document whose fields would go to review, and keeps the better result. none = off."},
        {"key": "CLAUDE_REGION", "label": "Processing region", "type": "text", "default": "",
         "help": "Used by the residency policy."},
        {"key": "CLAUDE_SEND_IMAGES", "label": "Send page images (better on scans)", "type": "bool", "default": "true"}],
        "install": "pip install anthropic"},
    "llm-local": {"fields": [
        {"key": "LOCAL_LLM_BASE_URL", "label": "Server URL", "type": "text", "default": "",
         "placeholder": "http://localhost:11434/v1", "help": "Ollama, vLLM, LM Studio..."},
        {"key": "LOCAL_LLM_MODEL", "label": "Model id", "type": "text", "default": ""}], "install": None},
}


def all_fields() -> list[dict]:
    out = list(DEPLOYMENT) + list(DEFAULTS)
    out += [{"key": p["key"], "type": "bool", "default": "true"} for p in PACKS.values()]
    for e in ENGINES.values():
        out += e["fields"]
    return out


SECRET_KEYS = {f["key"] for f in all_fields() if f["type"] == "secret"}
EDITABLE_KEYS = {f["key"] for f in all_fields()}


def env_path() -> Path:
    if os.environ.get("DOCAUTO_ENV_FILE"):
        return Path(os.environ["DOCAUTO_ENV_FILE"])
    cfg = os.environ.get("DOCAUTO_CONFIG")
    base = Path(cfg).parent if cfg else Path(__file__).resolve().parent.parent / "config"
    return base / "local.env"


_LINE = re.compile(r"^\s*([A-Z0-9_]+)=(.*)$")


def read_env(path: Path | None = None) -> dict[str, str]:
    path = path or env_path()
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def write_env(updates: dict[str, str], path: Path | None = None) -> Path:
    path = path or env_path()
    if not path.exists():
        example = path.with_name("local.env.example")
        if example.exists():
            shutil.copy(example, path)
        else:
            path.write_text("# Oneoffice Document Automation local settings\n", encoding="utf-8")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    pending = dict(updates)
    out = []
    for line in lines:
        m = _LINE.match(line)
        if m and m.group(1) in pending:
            out.append(f"{m.group(1)}={pending.pop(m.group(1))}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in pending.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def current_value(key: str, env_file: dict[str, str], default: str = "") -> str:
    # process env wins (start.ps1 loads local.env into it); file covers edits not yet applied
    v = os.environ.get(key)
    if v is None:
        v = env_file.get(key, default)
    return v


def _present(field: dict, env_file: dict[str, str]) -> dict:
    v = current_value(field["key"], env_file, field.get("default", ""))
    f = {k: v2 for k, v2 in field.items()}
    if field["type"] == "secret":
        f["isSet"] = bool(v)
        f["value"] = MASK if v else ""
    else:
        f["value"] = v
    return f


def describe() -> dict:
    env_file = read_env()
    return {
        "envFile": str(env_path()),
        "deployment": [_present(f, env_file) for f in DEPLOYMENT],
        "defaults": [_present(f, env_file) for f in DEFAULTS],
        "packs": {pid: {**p, "enabled": str(current_value(p["key"], env_file, "true")).lower() in ("true", "1", "yes")}
                  for pid, p in PACKS.items()},
        "engines": {eid: {"fields": [_present(f, env_file) for f in e["fields"]], "install": e["install"]}
                    for eid, e in ENGINES.items()},
    }


def apply(values: dict[str, Any]) -> dict[str, str]:
    """Validate, persist to local.env and the process environment. Returns what changed."""
    changed = {}
    for k, v in values.items():
        if k not in EDITABLE_KEYS:
            raise ValueError(f"'{k}' is not an editable setting")
        if k in SECRET_KEYS and v == MASK:
            continue  # unchanged secret
        if isinstance(v, bool):
            v = "true" if v else "false"
        v = "" if v is None else str(v).strip()
        if "\n" in v or "\r" in v:
            raise ValueError(f"'{k}' must be a single line")
        changed[k] = v
    if changed:
        write_env(changed)
        for k, v in changed.items():
            os.environ[k] = v
    return {k: (MASK if k in SECRET_KEYS and v else v) for k, v in changed.items()}
