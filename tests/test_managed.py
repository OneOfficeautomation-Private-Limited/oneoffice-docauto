"""Managed mode (DOCAUTO_MANAGED=true): DocFlow run by the OOA platform. No database needed."""
import pytest
from fastapi.testclient import TestClient

from docauto import managed, updates
from docauto.api.app import app

API = "/api/docs/v1"


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("DOCAUTO_MANAGED", "true")
    monkeypatch.setenv("DOCAUTO_API_TOKEN", "managed-token")
    return {"Authorization": "Bearer managed-token"}


def test_off_unless_asked(monkeypatch):
    monkeypatch.delenv("DOCAUTO_MANAGED", raising=False)
    assert not managed.enabled()
    for v in ("1", "true", "Yes", "on"):
        monkeypatch.setenv("DOCAUTO_MANAGED", v)
        assert managed.enabled()
    monkeypatch.setenv("DOCAUTO_MANAGED", "false")
    assert not managed.enabled()


def test_will_not_start_without_the_token(monkeypatch):
    monkeypatch.setenv("DOCAUTO_MANAGED", "true")
    monkeypatch.setenv("DOCAUTO_API_TOKEN", " ")
    with pytest.raises(RuntimeError, match="DOCAUTO_API_TOKEN"):
        managed.check_startup()
    monkeypatch.setenv("DOCAUTO_API_TOKEN", "t")
    managed.check_startup()
    monkeypatch.setenv("DOCAUTO_MANAGED", "false")
    monkeypatch.delenv("DOCAUTO_API_TOKEN")
    managed.check_startup()                         # standalone: as before


def test_no_self_update(on):
    c = TestClient(app)
    r = c.post(f"{API}/admin/update", headers=on)
    assert r.status_code == 409 and "managed by the OOA platform" in r.json()["detail"]
    assert c.get(f"{API}/admin/installer.bat", headers=on).status_code == 409
    with pytest.raises(RuntimeError, match="managed"):
        updates.start_update()


def test_version_says_managed_and_hides_the_folder(on):
    v = TestClient(app).get(f"{API}/admin/version").json()
    assert v["managed"] is True and v["canUpdate"] is False
    assert "folder" not in v and "repo" not in v
    assert v["version"]


def test_standalone_version_unchanged(monkeypatch):
    monkeypatch.delenv("DOCAUTO_MANAGED", raising=False)
    v = updates.version_info()
    assert v["managed"] is False and "folder" in v
