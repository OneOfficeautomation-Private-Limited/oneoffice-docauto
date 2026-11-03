import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).parent / "fixtures"
SAMPLES = ROOT / "samples"


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
    for k, v in over.items():
        cfg[k] = v
    return cfg


@pytest.fixture
def invoice_schema():
    from docauto.models import FieldSchema
    return FieldSchema.model_validate_json((SAMPLES / "invoice_schema.json").read_text(encoding="utf-8"))
