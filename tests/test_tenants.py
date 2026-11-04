"""Platform tenants: separate data and routing per tenant, behind the Command Center's service token."""
import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, make_config

DF = "/api/docs/v1/df"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DOCAUTO_FOLDER_WATCHER", "false")
    monkeypatch.setenv("DOCAUTO_CONFIG", str(ROOT / "config" / "document-service.yaml"))
    monkeypatch.delenv("DOCAUTO_API_TOKEN", raising=False)
    import docauto.api.app as appmod
    from docauto.pipeline import DocumentService
    appmod.set_service(DocumentService(make_config()))
    return TestClient(appmod.app)


def as_tenant(t):
    return {"X-OOA-Tenant": t, "X-OOA-User": "Sara Khan"}


def test_each_tenant_has_its_own_processes(client):
    r = client.post(f"{DF}/flows", json={"name": "AP invoices", "folder": "Finance / Accounts payable", "docTypes": ["invoice"]},
                    headers=as_tenant("acme"))
    assert r.status_code == 200 and r.json()["folder"] == "Finance / Accounts payable"
    acme = [f["name"] for f in client.get(f"{DF}/flows", headers=as_tenant("acme")).json()]
    other = [f["name"] for f in client.get(f"{DF}/flows", headers=as_tenant("globex")).json()]
    single = [f["name"] for f in client.get(f"{DF}/flows").json()]
    assert "AP invoices" in acme
    assert "AP invoices" not in other and "AP invoices" not in single


def test_tenant_data_lives_in_its_own_folder(client):
    import docauto.api.app as appmod
    client.get(f"{DF}/flows", headers=as_tenant("acme"))
    base = appmod._base_data_dir()
    assert appmod._services["acme"].config["storage"]["data_dir"].replace("\\", "/").endswith("tenants/acme")
    assert appmod._services["default"].config["storage"]["data_dir"] == base


def test_cloud_opt_in_is_per_tenant(client):
    r = client.put("/api/docs/v1/admin/settings", json={"values": {"DOCAUTO_CLOUD_OPT_IN": True}},
                   headers=as_tenant("acme"))
    assert r.status_code == 200, r.text
    assert client.get("/api/docs/v1/costs", headers=as_tenant("acme")).json()["cloudOptIn"] is True
    assert client.get("/api/docs/v1/costs", headers=as_tenant("globex")).json()["cloudOptIn"] is False
    s = client.get("/api/docs/v1/admin/settings", headers=as_tenant("acme")).json()
    opt = next(f for f in s["defaults"] if f["key"] == "DOCAUTO_CLOUD_OPT_IN")
    assert opt["value"] == "true" and opt["tenant"] is True and "envFile" not in s


def test_tenant_cannot_change_server_wide_settings(client):
    r = client.put("/api/docs/v1/admin/settings", json={"values": {"CLAUDE_API_KEY": "x"}}, headers=as_tenant("acme"))
    assert r.status_code == 400


def test_bad_tenant_id_is_refused(client):
    assert client.get(f"{DF}/flows", headers=as_tenant("../etc")).status_code == 400


def test_service_token(client, monkeypatch):
    monkeypatch.setenv("DOCAUTO_API_TOKEN", "s3cret")
    assert client.get(f"{DF}/flows", headers=as_tenant("acme")).status_code == 401
    ok = client.get(f"{DF}/flows", headers={**as_tenant("acme"), "Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_start_a_bot_calls_the_command_center_with_the_token_and_tenant(client, monkeypatch):
    """A platform "Start a bot" delivery goes to DOCAUTO_PLATFORM_URL as the flow's tenant; no secret sits in the flow."""
    import docauto.api.app as appmod
    import docauto.docflow as df
    monkeypatch.setenv("DOCAUTO_PLATFORM_URL", "http://cc.example/{tenant}")
    monkeypatch.setenv("DOCAUTO_API_TOKEN", "s3cret")
    sent = []

    class R:
        status_code = 200

    monkeypatch.setattr(df.httpx, "post", lambda url, json=None, headers=None, timeout=None: sent.append((url, json, headers)) or R())
    hdr = {**as_tenant("acme"), "Authorization": "Bearer s3cret"}
    fl = client.post(f"{DF}/flows", headers=hdr, json={"name": "AP", "docTypes": ["invoice"], "deliveries": [
        {"type": "start_bot", "name": "ERP", "on": ["success"], "params": {"bot": "Finance/Post.flow.json", "platform": True, "requestedBy": "sara"}}]}).json()
    svc = appmod._dfs["acme"]
    monkeypatch.setattr(svc, "payload", lambda f, d, s: {"documentId": d, "fields": {"total": 1}})
    out = svc._deliver(df.Flow.model_validate(fl), "doc1", "success")
    assert out[0]["ok"], out
    url, body, headers = sent[0]
    assert url == "http://cc.example/acme/api/docflow-callback/start-bot"
    assert headers["Authorization"] == "Bearer s3cret" and headers["X-OOA-Tenant"] == "acme"
    assert body["bot"] == "Finance/Post.flow.json" and body["requestedBy"] == "sara" and body["input"]["documentId"] == "doc1"


def test_start_a_bot_without_the_platform_address_says_so(client, monkeypatch):
    import docauto.docflow as df
    monkeypatch.delenv("DOCAUTO_PLATFORM_URL", raising=False)
    try:
        df.platform_callback("default")
        raise AssertionError("expected an error")
    except RuntimeError as e:
        assert "DOCAUTO_PLATFORM_URL" in str(e)
