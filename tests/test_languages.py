"""Languages of document types: codes mapped to each engine's own, what the engines here can read, and the
administrator's offered list (DOCAUTO_OFFERED_LANGUAGES), editable through the admin settings."""
import os
import shutil
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, load_option, make_config
from docauto import languages

API = "/api/docs/v1"


def test_codes_map_to_each_engine():
    assert languages.tesseract_code("ar") == "ara" and languages.tesseract_code("zh") == "chi_sim"
    assert languages.tesseract_code("xyz") == "xyz"                     # not in the table: passed on as it is
    assert languages.from_tesseract("urd") == "ur" and languages.from_tesseract("chi_tra") == "zh-Hant"
    assert languages.from_tesseract("osd") is None and languages.from_tesseract("script/Arabic") is None
    assert languages.from_tesseract("yor") == "yor"                     # an installed language not in the table
    assert languages.paddle_code("ar") == "arabic" and languages.paddle_code("bn") is None
    tess = load_option("tier1_opensource/tesseract/provider.py")
    assert tess.tess_langs(["ar", "ur", "ar"]) == "ara+urd"
    assert tess.tess_langs([]) == "eng"


def test_offered_list_parses_and_defaults():
    assert languages.offered({"languages": {"offered": "en, ar ,ur,en"}}) == ["en", "ar", "ur"]
    assert languages.offered({"languages": {"offered": None}}) == ["en", "ar"]
    assert languages.offered({}) == ["en", "ar"]


def _registry(**engines):
    """A registry with engines that read the given codes (None: the engine cannot run here)."""
    def entry(codes):
        if codes is None:
            return SimpleNamespace(available=False, instance=None)
        return SimpleNamespace(available=True, instance=SimpleNamespace(installed_languages=lambda: codes))
    return SimpleNamespace(get=lambda pid: entry(engines[pid]) if pid in engines else None)


def test_describe_marks_installed_and_offered():
    reg = _registry(tesseract=["en", "ar", "ur"], paddleocr=["en", "ar", "fr"])
    out = languages.describe({"languages": {"offered": "ar,en,hi"}}, reg)
    by = {x["code"]: x for x in out}
    assert [x["code"] for x in out[:3]] == ["ar", "en", "hi"]          # the offered ones first, in the admin's order
    assert by["en"] == {"code": "en", "name": "English", "engines": ["tesseract", "paddleocr"], "installed": True, "offered": True}
    assert by["hi"]["installed"] is False and by["hi"]["offered"] is True     # offered but no engine reads it here
    assert by["ur"] == {"code": "ur", "name": "Urdu", "engines": ["tesseract"], "installed": True, "offered": False}
    assert by["fr"]["engines"] == ["paddleocr"]
    assert by["ja"]["installed"] is False                              # the rest of the table, to choose from
    assert len(out) == len({x["code"] for x in out})


def test_engine_that_cannot_run_reads_nothing():
    out = languages.describe({}, _registry(tesseract=None, paddleocr=None))
    assert not any(x["installed"] for x in out)


def test_tesseract_lists_installed_language_data(monkeypatch):
    tess = load_option("tier1_opensource/tesseract/provider.py")
    pytesseract = pytest.importorskip("pytesseract")
    monkeypatch.setattr(pytesseract, "get_languages", lambda config="": ["ara", "eng", "osd", "urd", "chi_sim"])
    p = tess.TesseractProvider()
    assert p.installed_languages(fresh=True) == ["ar", "zh", "en", "ur"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    from docauto.api.app import app, set_service
    from docauto.pipeline import DocumentService
    env = tmp_path / "local.env"
    shutil.copy(ROOT / "config" / "local.env.example", env)
    monkeypatch.setenv("DOCAUTO_ENV_FILE", str(env))
    monkeypatch.setenv("DOCAUTO_CONFIG", str(ROOT / "config" / "document-service.yaml"))
    monkeypatch.delenv("DOCAUTO_OFFERED_LANGUAGES", raising=False)
    set_service(DocumentService(make_config()))
    yield TestClient(app)
    os.environ.pop("DOCAUTO_OFFERED_LANGUAGES", None)


def test_languages_endpoint_and_offered_setting(client):
    out = client.get(f"{API}/df/languages").json()
    assert [x["code"] for x in out if x["offered"]] == ["en", "ar"]
    assert {"code", "name", "engines", "installed", "offered"} <= set(out[0])
    if shutil.which("tesseract"):
        assert next(x for x in out if x["code"] == "en")["installed"]
    setting = next(f for f in client.get(f"{API}/admin/settings").json()["defaults"] if f["key"] == "DOCAUTO_OFFERED_LANGUAGES")
    assert setting["value"] == "en,ar"
    r = client.put(f"{API}/admin/settings", json={"values": {"DOCAUTO_OFFERED_LANGUAGES": "en,ar,ur"}})
    assert r.status_code == 200, r.text
    out = client.get(f"{API}/df/languages").json()
    assert [x["code"] for x in out if x["offered"]] == ["en", "ar", "ur"]
    assert next(x for x in out if x["code"] == "ur")["name"] == "Urdu"
