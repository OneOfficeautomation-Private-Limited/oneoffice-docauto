"""Document events to the OOA Command Center.

When a document in a process reaches a state, DocFlow tells the Command Center:

    POST {DOCAUTO_PLATFORM_URL}/api/docflow-callback/events
    Authorization: Bearer <DOCAUTO_API_TOKEN>      (the same scheme as "Start a bot" deliveries)
    X-OOA-Tenant: <url-encoded tenant>
    {"event": "processed" | "needs_review" | "approved" | "rejected" | "delivery_failed" | "review_overdue",
     "event_id": UUID (unique per event; the same on every retry of it),
     "document_id", "process_id", "process", "status", "file_name", "doc_type", "fields": {scalar fields},
     "at": ISO-8601 UTC}

DocFlow queues each event in its durable outbox (docauto.outbox) in the same transaction as the status change;
a background sender posts it with retries and exponential backoff, and marks it sent once (a delivered event is
never posted again). Nothing is queued when DOCAUTO_PLATFORM_URL is not set.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx

log = logging.getLogger("docauto.events")
EVENTS = ("processed", "needs_review", "approved", "rejected", "delivery_failed", "review_overdue")
PATH = "/api/docflow-callback/events"
RETRIES = 3            # emit(): attempts after the first one
BACKOFF = 2.0          # emit(): seconds, doubled after each failed attempt
TIMEOUT = 10


def platform_base(tenant: str) -> str:
    """The Command Center's address for a tenant ('' when not configured; {tenant} is replaced in SaaS)."""
    base = os.environ.get("DOCAUTO_PLATFORM_URL", "").strip().rstrip("/")
    return base.replace("{tenant}", tenant) if base else ""


def platform_auth() -> dict[str, str]:
    """This service's own token: the Command Center checks it on its callbacks, so no secret is kept per process."""
    token = os.environ.get("DOCAUTO_API_TOKEN")
    return {"Authorization": "Bearer " + token} if token else {}


def body(event: str, document_id: str, process_id: str, process: str, status: str, file_name: Optional[str],
         doc_type: Optional[str], fields: dict[str, Any]) -> dict:
    if event not in EVENTS:
        raise ValueError(f"unknown event '{event}'")
    return {"event": event, "event_id": str(uuid.uuid4()), "document_id": document_id, "process_id": process_id,
            "process": process, "status": status, "file_name": file_name or "", "doc_type": doc_type,
            "fields": {k: v for k, v in fields.items() if not isinstance(v, (list, dict))},
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}


def send(tenant: str, payload: dict) -> tuple[bool, str, bool]:
    """One attempt. Returns (delivered, detail, worth retrying). Used by the outbox sender."""
    base = platform_base(tenant)
    if not base:
        return False, "DOCAUTO_PLATFORM_URL is not set", False
    headers = {"Content-Type": "application/json", **platform_auth(), "X-OOA-Tenant": quote(tenant, safe="")}
    try:
        r = httpx.post(base + PATH, json=payload, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}: {e}", True
    if r.status_code < 300:
        return True, f"HTTP {r.status_code}", False
    # the Command Center refused it (4xx other than timeout / too many requests): retrying will not help
    return False, f"HTTP {r.status_code}", not (400 <= r.status_code < 500 and r.status_code not in (408, 429))


def _send(tenant: str, payload: dict) -> bool:
    delay, detail = BACKOFF, ""
    for attempt in range(RETRIES + 1):
        ok, detail, retry = send(tenant, payload)
        if ok:
            return True
        if not retry:
            break
        if attempt < RETRIES:
            time.sleep(delay)
            delay *= 2
    log.warning("event %s for %s not delivered: %s", payload.get("event"), payload.get("document_id"), detail)
    return False


def emit(tenant: str, payload: dict) -> Optional[threading.Thread]:
    """Send one event now, in the background, without the outbox (tools and tests; DocFlow queues its events).
    Returns the sending thread (None when no Command Center is set)."""
    if not platform_base(tenant):
        return None
    t = threading.Thread(target=_send, args=(tenant, payload), name="docflow-event", daemon=True)
    t.start()
    return t
