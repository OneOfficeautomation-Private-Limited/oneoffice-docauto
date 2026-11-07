"""Version info and GitHub install/update helpers for the web UI (Settings > Updates).

The actual install/update logic lives in windows/get-docflow.ps1; the service only reports
what is installed, opens the updater window on Windows, and hands out a ready-to-share installer.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import managed
from . import settings as settings_mod

ROOT = Path(__file__).resolve().parent.parent
WIN = ROOT / "windows"


def version_info() -> dict:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "dev"
    installed: dict = {}
    vf = ROOT / ".docflow-version"
    if vf.exists():
        try:
            installed = json.loads(vf.read_text(encoding="utf-8-sig"))
        except ValueError:
            installed = {}
    env = settings_mod.read_env()
    repo = os.environ.get("DOCFLOW_REPO") or env.get("DOCFLOW_REPO", "") or installed.get("repo", "")
    ref = os.environ.get("DOCFLOW_REF") or env.get("DOCFLOW_REF", "") or installed.get("ref", "") or "main"
    commit = installed.get("commit", "")
    if managed.enabled():   # the platform installs and upgrades it; the folder is nobody's business without a token
        return {"version": version, "commit": commit[:7], "managed": True, "canUpdate": False}
    return {
        "version": version,
        "managed": False,
        "commit": commit[:7],
        "installedAt": installed.get("installedAt", ""),
        "source": installed.get("source", ""),
        "repo": repo,
        "ref": ref,
        "gitWorkingCopy": (ROOT / ".git").exists(),
        "canUpdate": os.name == "nt" and (WIN / "update.bat").exists(),
        "folder": str(ROOT),
    }


def start_update() -> None:
    """Open the updater in its own console window (Windows). It stops this service and restarts it."""
    if managed.enabled():
        raise RuntimeError(managed.NO_SELF_UPDATE)
    if os.name != "nt":
        raise RuntimeError("one-click update is available on Windows installs; elsewhere run windows/get-docflow.ps1 or git pull")
    bat = WIN / "update.bat"
    if not bat.exists():
        raise RuntimeError("windows\\update.bat not found")
    os.startfile(str(bat))  # type: ignore[attr-defined]  # new console window, independent of this process


def installer_bat(repo: str, ref: str) -> str:
    text = (WIN / "Install-OneOffice-DocFlow.bat").read_text(encoding="utf-8")
    if repo and re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        text = text.replace('set "DOCFLOW_REPO="', f'set "DOCFLOW_REPO={repo}"', 1)
    if ref and re.fullmatch(r"[\w./-]+", ref):
        text = text.replace('set "DOCFLOW_REF=main"', f'set "DOCFLOW_REF={ref}"', 1)
    return text.replace("\r\n", "\n").replace("\n", "\r\n")
