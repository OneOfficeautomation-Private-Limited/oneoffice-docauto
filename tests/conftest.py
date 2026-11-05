import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).parent / "fixtures"
SAMPLES = ROOT / "samples"


def _local_tesseract():
    """Tesseract installed but not on PATH (Windows): take TESSERACT_CMD / TESSDATA_PREFIX from config/local.env
    (read only), like windows/start.ps1 does, so the OCR end-to-end tests run instead of being skipped."""
    env = ROOT / "config" / "local.env"
    if shutil.which("tesseract") or not env.exists():
        return
    vals = dict(re.findall(r"(?m)^\s*(TESSERACT_CMD|TESSDATA_PREFIX)=(.*?)\s*$", env.read_text(encoding="utf-8-sig")))
    cmd = vals.get("TESSERACT_CMD")
    if cmd and Path(cmd).exists():
        os.environ["PATH"] = str(Path(cmd).parent) + os.pathsep + os.environ.get("PATH", "")
        if vals.get("TESSDATA_PREFIX") and Path(vals["TESSDATA_PREFIX"]).is_dir():
            os.environ.setdefault("TESSDATA_PREFIX", vals["TESSDATA_PREFIX"])


_local_tesseract()


@pytest.fixture(scope="session", autouse=True)
def database():
    """The whole suite runs on PostgreSQL: DOCAUTO_TEST_DATABASE_URL (CI service), else an embedded server in a
    temporary folder (pgserver), deleted afterwards. Each test service gets its own schemas (make_config)."""
    url = os.environ.get("DOCAUTO_TEST_DATABASE_URL")
    tmp = None
    if not url:
        import pgserver
        tmp = tempfile.mkdtemp(prefix="docauto-pg-")
        srv = pgserver.get_server(tmp, cleanup_mode="delete")
        from docauto import db
        db._ensure_database(srv.get_uri(), "docflow_test")
        url = srv.get_uri(database="docflow_test")
    os.environ["DOCAUTO_DATABASE_URL"] = url
    yield url
    from docauto import outbox, pipeline
    outbox.stop_all()
    pipeline.close_all()


def load_option(rel: str):
    """Import an option pack module by path, e.g. 'tier2_cloud/azure_di/provider.py'."""
    path = ROOT / "options" / rel
    spec = importlib.util.spec_from_file_location("opt_" + rel.replace("/", "_").replace(".py", ""), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_json(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session", autouse=True)
def samples():
    if not (SAMPLES / "invoice_scan.png").exists():
        sys.path.insert(0, str(SAMPLES))
        import make_samples
        make_samples.main(SAMPLES)
    return SAMPLES


def make_config(**over):
    from docauto.config import load_config
    cfg = load_config(ROOT / "config" / "document-service.yaml")
    cfg["storage"]["data_dir"] = tempfile.mkdtemp(prefix="docauto-test-")
    cfg["storage"]["schema_prefix"] = f"t{uuid.uuid4().hex[:10]}_"   # fresh schemas: tests never share data
    for k, v in over.items():
        cfg[k] = v
    return cfg


@pytest.fixture
def invoice_schema():
    from docauto.models import FieldSchema
    return FieldSchema.model_validate_json((SAMPLES / "invoice_schema.json").read_text(encoding="utf-8"))
