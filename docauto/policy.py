"""Deployment profile enforcement: decides which engines may receive a document."""
from __future__ import annotations

from fnmatch import fnmatch

from .provider import ProviderInfo

PROFILES = ("onprem_airgapped", "onprem_hybrid", "cloud_saas")


def _as_list(v) -> list[str]:
    """Accept YAML lists or comma-separated strings (from environment variables)."""
    if not v:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


class PolicyEngine:
    def __init__(self, config: dict):
        self.profile = config.get("deployment_profile", "onprem_airgapped")
        if self.profile not in PROFILES:
            raise ValueError(f"unknown deployment_profile '{self.profile}', expected one of {PROFILES}")
        prov = config.get("providers", {})
        self.allow = _as_list(prov.get("allow")) or ["*"]
        self.deny = _as_list(prov.get("deny"))
        res = config.get("residency", {})
        self.allowed_regions = _as_list(res.get("allowed_regions"))
        self.allow_cross_border = bool(res.get("allow_cross_border", False))
        # client consent for outbound engines; the shipped config starts local-only (OCR strategy plan)
        opt_in = (config.get("routing") or {}).get("cloud_opt_in", True)
        self.cloud_opt_in = opt_in if isinstance(opt_in, bool) else str(opt_in).strip().lower() in ("true", "1", "yes")

    def check(self, info: ProviderInfo) -> tuple[bool, str]:
        if any(fnmatch(info.id, d) for d in self.deny):
            return False, f"'{info.id}' is on the deny list"
        if not any(fnmatch(info.id, a) for a in self.allow):
            return False, f"'{info.id}' is not on the allow list"
        if info.runsLocally:
            return True, "local engine"
        if self.profile == "onprem_airgapped":
            return False, "profile onprem_airgapped forbids outbound engines"
        if not self.cloud_opt_in:
            return False, "cloud opt-in is off: documents stay on this machine"
        if self.allowed_regions and not self.allow_cross_border:
            if not info.regions or not any(r in self.allowed_regions for r in info.regions):
                return False, (f"'{info.id}' regions {info.regions or ['unknown']} not in allowed "
                               f"regions {self.allowed_regions}")
        return True, "allowed"

    def allows(self, info: ProviderInfo) -> bool:
        return self.check(info)[0]
