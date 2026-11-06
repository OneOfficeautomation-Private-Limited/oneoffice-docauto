"""Platform contract v1 (PLATFORM-CONTRACT.md): what the OOA Command Center relies on.

These tests need no database, Tesseract or network: they check the routes, headers, version labels and callback
shapes the platform uses. If one fails, the change would break AI > Document Automation in the platform. Either keep
the old behaviour (v1 only grows) or start /api/docs/v2.
"""
import re
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docauto import __version__, events
from docauto.api.app import app
from docauto.docflow import platform_callback

ROOT = Path(__file__).resolve().parent.parent
API = "/api/docs/v1"

# (method, route under /api/docs/v1) as the Command Center calls them: platform/ui/src/docflow/http.ts and
# DocFlowProxy.java. Path parameters are written as {x}.
PLATFORM_ROUTES = [
    ("GET", "df/overview"),
    ("GET", "df/flows"),
    ("POST", "df/flows"),
    ("GET", "df/flows/{x}"),
    ("PUT", "df/flows/{x}"),
    ("POST", "df/flows/{x}/live"),
    ("GET", "df/flows/{x}/documents"),
    ("POST", "df/flows/{x}/documents"),
    ("GET", "df/documents/{x}"),
    ("POST", "df/documents/{x}/review"),
    ("POST", "df/documents/{x}/reclassify"),
    ("GET", "df/review"),
    ("GET", "df/doctypes"),
    ("POST", "df/doctypes"),
    ("GET", "df/doctypes/{x}"),
    ("PUT", "df/doctypes/{x}"),
    ("DELETE", "df/doctypes/{x}"),
    ("POST", "df/doctypes/suggest"),
    ("GET", "df/library"),
    ("GET", "df/reftables"),
    ("POST", "df/reftables"),
    ("GET", "df/reftables/{x}"),
    ("DELETE", "df/reftables/{x}"),
    ("POST", "df/test"),
    ("GET", "providers"),
    ("GET", "costs"),
    ("GET", "usage"),
    ("GET", "admin/settings"),
    ("PUT", "admin/settings"),
    ("POST", "admin/providers/{x}/test"),
    ("GET", "documents/{x}/result"),
    ("GET", "documents/{x}/page/{x}.png"),
    ("GET", "documents/{x}/file"),
]


def _served() -> set[tuple[str, str]]:
    out = set()
    for r in app.routes:
        path = getattr(r, "path", "")
        if not path.startswith(API + "/"):
            continue
        route = re.sub(r"\{[^}]+\}", "{x}", path[len(API) + 1:])
        for m in getattr(r, "methods", set()):
            out.add((m, route))
    return out


@pytest.mark.parametrize("method,route", PLATFORM_ROUTES, ids=[f"{m} {r}" for m, r in PLATFORM_ROUTES])
def test_platform_route_is_served(method, route):
    assert (method, route) in _served(), f"the platform calls {method} {API}/{route}, which is no longer served"


def _route(method: str, path: str):
    return next(r for r in app.routes if getattr(r, "path", "") == API + "/" + path and method in r.methods)


def _params(method: str, path: str) -> set[str]:
    dep = _route(method, path).dependant
    return {p.alias for p in dep.query_params + dep.body_params + dep.path_params}


def test_upload_takes_the_inputs_the_platform_sends():
    # browser uploads, the email poller and "Send document to process" all post multipart "files"
    assert {"files", "source", "requireLive", "wait"} <= _params("POST", "df/flows/{flow_id}/documents")
    assert "files" in _params("POST", "df/doctypes/suggest")
    assert {"file", "docType", "engines"} <= _params("POST", "df/test")
    assert {"limit", "fields"} <= _params("GET", "df/flows/{flow_id}/documents")
    assert "live" in _params("POST", "df/flows/{flow_id}/live")
    assert "groupBy" in _params("GET", "usage")


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("DOCAUTO_API_TOKEN", "contract-token")
    return "contract-token"


def test_wrong_or_missing_token_is_401(token):
    c = TestClient(app)
    assert c.get(f"{API}/df/flows").status_code == 401
    assert c.get(f"{API}/df/flows", headers={"Authorization": "Bearer other"}).status_code == 401


def test_bad_tenant_is_400_before_anything_else(token):
    r = TestClient(app).get(f"{API}/df/flows", headers={"Authorization": f"Bearer {token}", "X-OOA-Tenant": "Not A Tenant!"})
    assert r.status_code == 400 and r.json()["code"] == "BAD_TENANT"


def test_version_needs_no_token_and_matches(token):
    r = TestClient(app).get(f"{API}/admin/version")
    assert r.status_code == 200 and r.json()["version"] == __version__


def test_version_labels_agree():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert __version__ == version, "docauto/__init__.py"
    assert tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"] == version, "pyproject.toml"
    assert app.version == version, "FastAPI app"
    assert f"oneoffice/docauto:{version}" in (ROOT / "docker-compose.yml").read_text(encoding="utf-8"), "docker-compose.yml image tag"


def test_start_bot_callback_goes_to_the_command_center(monkeypatch, token):
    monkeypatch.setenv("DOCAUTO_PLATFORM_URL", "https://{tenant}.cc.example/")
    url, headers = platform_callback("acme")
    assert url == "https://acme.cc.example/api/docflow-callback/start-bot"
    assert headers == {"X-OOA-Tenant": "acme", "Authorization": f"Bearer {token}"}


def test_event_body_has_the_fields_the_platform_reads():
    b = events.body("needs_review", "doc1", "p1", "AP invoices", "validation", "a.pdf", "invoice",
                    {"total": 10, "lines": [1, 2]})
    assert set(b) == {"event", "event_id", "document_id", "process_id", "process", "status", "file_name", "doc_type",
                      "fields", "at"}
    assert b["fields"] == {"total": 10}           # only plain values, never tables
    assert events.PATH == "/api/docflow-callback/events"
