"""Settings > Updates: version info, installer download, update guard on non-Windows."""
import os

from fastapi.testclient import TestClient

from docauto import updates
from docauto.api.app import app

client = TestClient(app)
API = "/api/docs/v1"


def test_version_endpoint():
    v = client.get(f"{API}/admin/version").json()
    assert v["version"] == (updates.ROOT / "VERSION").read_text().strip()
    assert {"repo", "ref", "commit", "canUpdate", "folder"} <= set(v)


def test_installer_has_repo_and_crlf(monkeypatch):
    monkeypatch.setenv("DOCFLOW_REPO", "acme/oneoffice-docauto")
    r = client.get(f"{API}/admin/installer.bat")
    assert r.status_code == 200
    text = r.content.decode()
    assert 'set "DOCFLOW_REPO=acme/oneoffice-docauto"' in text
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert "get-docflow.ps1" in text


def test_installer_rejects_injected_repo():
    text = updates.installer_bat('x" & del *', "main")
    assert 'set "DOCFLOW_REPO="' in text and "del *" not in text


def test_update_only_on_windows():
    if os.name == "nt":  # would really open the updater on a Windows PC
        return
    assert client.post(f"{API}/admin/update").status_code == 409


def test_old_brand_assets_are_gone():
    assert client.get("/ui/brand/ooa-mark.svg").status_code == 404
