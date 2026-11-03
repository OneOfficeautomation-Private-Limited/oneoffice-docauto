"""Discovers and loads engine option packs from options/<pack>/<provider>/manifest.json.

Each provider folder is self-contained (manifest + provider.py), so a pack can be
shipped, licensed and installed separately from the core.
"""
from __future__ import annotations

import importlib.util
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .provider import BaseProvider, Capability, ProviderContext, ProviderInfo

log = logging.getLogger("docauto.registry")


@dataclass
class RegisteredProvider:
    info: ProviderInfo
    instance: Optional[BaseProvider]
    available: bool
    reason: str = ""
    path: str = ""


def _info_from_manifest(m: dict, pack: str) -> ProviderInfo:
    return ProviderInfo(
        id=m["id"], displayName=m.get("displayName", m["id"]), version=m.get("version", "0.0.0"),
        vendor=m.get("vendor", ""), tier=int(m.get("tier", 1)), kind=m.get("kind", "ocr"),
        capabilities=[Capability(c) for c in m.get("capabilities", [])],
        languages=m.get("languages", ["*"]), runsLocally=bool(m.get("runsLocally", False)),
        regions=m.get("regions", []), creditCostPerPage=m.get("creditCostPerPage", {}),
        supportedMimeTypes=m.get("supportedMimeTypes", []),
        maxPagesPerRequest=m.get("maxPagesPerRequest"), credentialTypes=m.get("credentialTypes", []),
        optionPack=pack,
    )


class ProviderRegistry:
    def __init__(self, config: dict):
        self.config = config
        self.providers: dict[str, RegisteredProvider] = {}

    def register(self, instance: BaseProvider, info: ProviderInfo, settings: dict | None = None) -> None:
        """Programmatic registration (built-ins and tests)."""
        instance.info = info
        settings = settings or {}
        instance.init(ProviderContext(config=settings.get("config", {}),
                                      credentials=settings.get("credentials", {})))
        self.providers[info.id] = RegisteredProvider(info, instance, True, path="builtin")

    def discover(self) -> None:
        root = Path(self.config["options_dir"])
        enabled_packs = {k for k, v in self.config.get("option_packs", {}).items() if v}
        for manifest_path in sorted(root.glob("*/*/manifest.json")):
            pack = manifest_path.parent.parent.name
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                info = _info_from_manifest(manifest, pack)
            except Exception as e:  # malformed manifest
                log.error("bad manifest %s: %s", manifest_path, e)
                continue
            if pack not in enabled_packs:
                self.providers[info.id] = RegisteredProvider(info, None, False,
                                                             f"option pack '{pack}' not enabled", str(manifest_path.parent))
                continue
            self._load(manifest, info, manifest_path.parent)

    def _load(self, manifest: dict, info: ProviderInfo, folder: Path) -> None:
        settings = self.config.get("providers", {}).get("settings", {}).get(info.id, {})
        try:
            module_file = folder / manifest.get("module", "provider.py")
            spec = importlib.util.spec_from_file_location(f"docauto_option_{info.id.replace('-', '_')}", module_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            cls = getattr(module, manifest["entrypoint"])
            inst: BaseProvider = cls()
            inst.info = info
            inst.init(ProviderContext(config=settings.get("config", {}),
                                      credentials=settings.get("credentials", {})))
            health = inst.health_check()
            self.providers[info.id] = RegisteredProvider(info, inst, health.ok, health.message, str(folder))
        except Exception as e:
            log.warning("provider %s unavailable: %s", info.id, e)
            self.providers[info.id] = RegisteredProvider(info, None, False, f"{type(e).__name__}: {e}", str(folder))

    def get(self, provider_id: str) -> Optional[RegisteredProvider]:
        return self.providers.get(provider_id)

    def available(self, kind: str | None = None) -> list[RegisteredProvider]:
        return [p for p in self.providers.values() if p.available and (kind is None or p.info.kind == kind)]

    def describe(self) -> list[dict]:
        return [{**p.info.to_dict(), "available": p.available, "reason": p.reason}
                for p in sorted(self.providers.values(), key=lambda p: (p.info.tier, p.info.id))]
