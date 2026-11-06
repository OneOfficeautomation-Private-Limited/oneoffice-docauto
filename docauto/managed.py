"""Managed mode: DocFlow run by the OOA platform (its installer, its containers or a SaaS region).

Turned on with DOCAUTO_MANAGED=true; standalone installs keep working as before. In managed mode DocFlow:

* refuses to start without DOCAUTO_API_TOKEN, because without it the API would answer anyone who can reach it;
* does not update itself: the platform installs and upgrades DocFlow with the version its release was tested with
  (the self-updater would pull whatever GitHub has);
* does not show its install folder in GET /api/docs/v1/admin/version, which needs no token.
"""
from __future__ import annotations

import os

NO_SELF_UPDATE = ("DocFlow is managed by the OOA platform: it is updated with the platform's releases, "
                  "not from here")


def enabled() -> bool:
    return os.environ.get("DOCAUTO_MANAGED", "").strip().lower() in ("1", "true", "yes", "on")


def check_startup() -> None:
    """Raises when managed mode is on but the service token is missing (the service then does not start)."""
    if enabled() and not os.environ.get("DOCAUTO_API_TOKEN", "").strip():
        raise RuntimeError("DOCAUTO_MANAGED is on but DOCAUTO_API_TOKEN is empty: set the service token (the same one "
                           "the OOA Command Center has) so the API is not open to anyone")
