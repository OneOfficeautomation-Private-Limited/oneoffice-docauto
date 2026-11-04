"""Admin settings API used by the web UI: read, save (local.env), live reload, engine test."""
import os
import shutil

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, make_config

API = "/api/docs/v1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from docauto.api.app import app, set_service
    from docauto.pipeline import DocumentService
    env = tmp_path / "local.env"
    shutil.copy(ROOT / "config" / "local.env.example", env)
    monkeypatch.setenv("DOCAUTO_ENV_FILE", str(env))
    monkeypatch.setenv("DOCAUTO_CONFIG", str(ROOT / "config" / "document-service.yaml"))
    for k in ("DOCAUTO_PROFILE", "DOCAUTO_PACK_CLOUD", "AZURE_DI_KEY", "AZURE_DI_ENDPOINT", "DOCAUTO_DENY"):
        monkeypatch.delenv(k, raising=False)
    set_service(DocumentService(make_config()))
    c = TestClient(app)
    c.env = env
    yield c
    for k in ("DOCAUTO_PROFILE", "DOCAUTO_PACK_CLOUD", "AZURE_DI_KEY", "AZURE_DI_ENDPOINT", "AZURE_DI_REGION", "DOCAUTO_DENY"):
        os.environ.pop(k, None)


def test_no_screens_of_its_own(client):
    """The screens are in the OOA Command Center; DocFlow answers its address with where to find them."""
    root = client.get("/").json()
    assert root["service"] == "OneOffice DocFlow" and "Command Center" in root["screens"]
    for old in ("/ui", "/validation", "/ui/docflow.js", "/favicon.ico"):
        assert client.get(old).status_code == 404


def test_settings_roundtrip_masks_secrets_and_reloads(client):
    s = client.get(f"{API}/admin/settings").json()
    assert set(s["packs"]) == {"tier1_opensource", "tier2_cloud", "tier3_ai"}
    r = client.put(f"{API}/admin/settings", json={"values": {
        "AZURE_DI_ENDPOINT": "https://x.cognitiveservices.azure.com", "AZURE_DI_KEY": "secret-123",
        "AZURE_DI_REGION": "uaenorth"}})
    assert r.status_code == 200, r.text
    assert r.json()["changed"]["AZURE_DI_KEY"] != "secret-123"          # never echoed back
    azure = next(p for p in r.json()["providers"] if p["id"] == "azure-di")
    assert azure["available"] and azure["regions"] == ["uaenorth"]     # live reload picked it up
    text = client.env.read_text(encoding="utf-8")
    assert "AZURE_DI_KEY=secret-123" in text and "# --- option 2" in text  # comments preserved
    eng = client.get(f"{API}/admin/settings").json()["engines"]["azure-di"]["fields"]
    key = next(f for f in eng if f["key"] == "AZURE_DI_KEY")
    assert key["isSet"] and key["value"] != "secret-123"
    # sending the mask back keeps the stored secret
    client.put(f"{API}/admin/settings", json={"values": {"AZURE_DI_KEY": key["value"]}})
    assert "AZURE_DI_KEY=secret-123" in client.env.read_text(encoding="utf-8")


def test_profile_and_deny_apply_live(client):
    r = client.put(f"{API}/admin/settings", json={"values": {"DOCAUTO_PROFILE": "onprem_airgapped",
                                                             "DOCAUTO_DENY": "paddleocr"}}).json()
    assert r["profile"] == "onprem_airgapped"
    pol = {p["id"]: p for p in r["providers"]}
    assert not pol["azure-di"]["allowedByPolicy"] and not pol["paddleocr"]["allowedByPolicy"]
    assert pol["tesseract"]["allowedByPolicy"]


def test_rejects_unknown_or_multiline(client):
    assert client.put(f"{API}/admin/settings", json={"values": {"PATH": "x"}}).status_code == 400
    assert client.put(f"{API}/admin/settings", json={"values": {"LLM_MODEL": "a\nB=1"}}).status_code == 400


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_engine_test_endpoint(client):
    r = client.post(f"{API}/admin/providers/tesseract/test").json()
    assert r["ok"] and "12345" in r["output"]
    r = client.post(f"{API}/admin/providers/builtin-rules/test").json()
    assert r["ok"]
    r = client.post(f"{API}/admin/providers/llm-cloud/test").json()
    assert not r["ok"] and r["stage"] in ("configuration", "policy")
