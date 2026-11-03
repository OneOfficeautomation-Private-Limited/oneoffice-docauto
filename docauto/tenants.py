"""Tenants of the host platform (OOA Command Center).

The Command Center forwards each request with the signed-in user's tenant (X-OOA-Tenant) and user (X-OOA-User),
authenticated by the service token. Each tenant gets its own DocFlow data: database and files under
<data_dir>/tenants/<tenant>, and its own routing settings (cloud opt-in, retry chain, review thresholds) in
tenant-settings.json there. Engine keys and the deployment profile stay server-wide. The tenant "default" is a
single-client install: it keeps the base data folder and config/local.env, as before tenants existed.
"""
from __future__ import annotations

import json
import re
from contextvars import ContextVar
from pathlib import Path

from . import settings

DEFAULT = "default"
TENANT_HEADER = b"x-ooa-tenant"
USER_HEADER = b"x-ooa-user"
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

current: ContextVar[str] = ContextVar("docauto_tenant", default=DEFAULT)
current_user: ContextVar[str] = ContextVar("docauto_user", default="")

# what a tenant may set for itself: routing, default engines and review thresholds (not keys or the profile)
TENANT_KEYS = {f["key"] for f in settings.DEFAULTS}


def valid(tenant: str) -> bool:
    return bool(_ID.match(tenant))


def data_dir(base: str | Path, tenant: str) -> Path:
    if tenant == DEFAULT:
        return Path(base)
    if not valid(tenant):
        raise ValueError(f"'{tenant}' is not a tenant id")
    return Path(base) / "tenants" / tenant


def known(base: str | Path) -> list[str]:
    """Tenants that already have data, so their folder watchers start with the service."""
    root = Path(base) / "tenants"
    return sorted(p.name for p in root.iterdir() if p.is_dir() and valid(p.name)) if root.is_dir() else []


def _settings_file(base: str | Path, tenant: str) -> Path:
    return data_dir(base, tenant) / "tenant-settings.json"


def read_overrides(base: str | Path, tenant: str) -> dict[str, str]:
    path = _settings_file(base, tenant)
    if tenant == DEFAULT or not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: str(v) for k, v in raw.items() if k in TENANT_KEYS}


def write_overrides(base: str | Path, tenant: str, values: dict) -> dict[str, str]:
    """Validate and save a tenant's own settings. Returns what changed."""
    current_values = read_overrides(base, tenant)
    changed = {}
    for k, v in values.items():
        if k not in TENANT_KEYS:
            raise ValueError(f"'{k}' is set for the whole server, not per tenant")
        if isinstance(v, bool):
            v = "true" if v else "false"
        v = "" if v is None else str(v).strip()
        if "\n" in v or "\r" in v:
            raise ValueError(f"'{k}' must be a single line")
        if current_values.get(k) != v:
            changed[k] = v
    if changed:
        path = _settings_file(base, tenant)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**current_values, **changed}, indent=2), encoding="utf-8")
    return changed


class TenantMiddleware:
    """Pure ASGI, so the tenant reaches the endpoint (sync endpoints run in a copy of this context)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        tenant = headers.get(TENANT_HEADER, b"").decode("latin-1").strip().lower() or DEFAULT
        if tenant != DEFAULT and not valid(tenant):
            body = json.dumps({"code": "BAD_TENANT", "message": "invalid tenant id"}).encode()
            await send({"type": "http.response.start", "status": 400,
                        "headers": [(b"content-type", b"application/json")]})
            return await send({"type": "http.response.body", "body": body})
        user = headers.get(USER_HEADER, b"").decode("utf-8", "replace").strip()[:200]
        t1, t2 = current.set(tenant), current_user.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            current.reset(t1)
            current_user.reset(t2)
