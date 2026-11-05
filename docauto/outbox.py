"""Durable outbox: deliveries (webhook, folder, excel, start_bot) and document events (docauto.events).

Items are written to the tenant's `outbox` table in the same transaction as the status change that causes them
(Store.outbox_add inside Store.tx), so a crash can neither lose them nor send something that was rolled back.
A background Sender per tenant leases due items (FOR UPDATE SKIP LOCKED, so several processes can send safely),
sends each with its kind's handler, and then:

* delivered            -> marked 'sent' once (a delivered item is never sent again),
* failed, retryable    -> retried after BACKOFF * 2^(attempt-1) seconds (at most MAX_DELAY), up to MAX_ATTEMPTS,
* failed for good      -> marked 'failed' and on_failed(item, detail) runs (DocFlow then emits "delivery_failed").

A sender that stops mid-send leaves the item leased; after LEASE seconds another sender takes it over (a send that
was cut off may then be repeated: at-least-once for the receiver, but never re-sent after it was marked sent).
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
import weakref
from typing import Callable, Optional

from .store import Store

log = logging.getLogger("docauto.outbox")
MAX_ATTEMPTS = int(os.environ.get("DOCAUTO_OUTBOX_MAX_ATTEMPTS") or 10)
BACKOFF = float(os.environ.get("DOCAUTO_OUTBOX_BACKOFF") or 10.0)     # seconds before the first retry
MAX_DELAY = 900.0                                                    # 15 minutes between attempts at most
LEASE = 300.0
POLL_MIN, POLL_MAX = 0.2, 5.0

Handler = Callable[[dict], tuple[bool, str, bool]]     # item -> (sent, detail, worth retrying)
_senders: "weakref.WeakSet[Sender]" = weakref.WeakSet()


def stop_all() -> None:
    """Stop every sender of this process (shutdown, tests)."""
    for s in list(_senders):
        s.stop()


def delay_for(attempt: int) -> float:
    """Seconds before the next try after the attempt-th failure (1-based)."""
    return min(MAX_DELAY, BACKOFF * (2 ** max(0, attempt - 1)))


class Sender:
    def __init__(self, store: Store, handlers: dict[str, Handler],
                 on_sent: Optional[Callable[[dict, str], None]] = None,
                 on_failed: Optional[Callable[[dict, str], None]] = None, name: str = "docflow-outbox"):
        self.store, self.handlers, self.on_sent, self.on_failed = store, handlers, on_sent, on_failed
        self.name = name
        self.claim = uuid.uuid4().hex
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread: Optional[threading.Thread] = None
        _senders.add(self)

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        idle = POLL_MIN
        while not self._stop.is_set():
            try:
                n = self.run_once()
            except Exception:
                log.exception("outbox sender failed")
                n = 0
            if n:
                idle = POLL_MIN
                continue
            self._wake.wait(idle)
            if self._wake.is_set():
                self._wake.clear()
                idle = POLL_MIN
            else:
                idle = min(idle * 2, POLL_MAX)

    def run_once(self, limit: int = 10) -> int:
        """Send what is due now; returns how many items were tried."""
        items = self.store.outbox_claim(self.claim, limit, LEASE)
        for item in items:
            if self._stop.is_set():
                break
            self._send(item)
        return len(items)

    def _send(self, item: dict) -> None:
        handler = self.handlers.get(item["kind"])
        try:
            ok, detail, retry = handler(item) if handler else (False, f"no handler for '{item['kind']}'", False)
        except Exception as e:   # a handler bug must not stop the sender
            log.exception("outbox item %s (%s) crashed", item["id"], item["kind"])
            ok, detail, retry = False, f"{type(e).__name__}: {e}", True
        attempt = item["attempts"] + 1
        if ok:
            if self.store.outbox_sent(item["id"], self.claim) and self.on_sent:
                self._call(self.on_sent, item, detail)
            return
        if retry and attempt < MAX_ATTEMPTS:
            delay = delay_for(attempt)
            log.info("outbox %s %s for %s failed (%s), retry %d in %.0f s", item["kind"], item["name"],
                     item["document_id"], detail, attempt, delay)
            self.store.outbox_retry(item["id"], self.claim, detail, delay)
            return
        log.warning("outbox %s %s for %s failed for good after %d attempt(s): %s", item["kind"], item["name"],
                    item["document_id"], attempt, detail)
        if self.on_failed is None:
            self.store.outbox_failed(item["id"], self.claim, detail)
            return
        with self.store.tx():    # failed + whatever on_failed queues (the "delivery_failed" event): together
            if self.store.outbox_failed(item["id"], self.claim, detail):
                self.on_failed(item, f"{detail} (after {attempt} attempt{'s' if attempt > 1 else ''})")

    def _call(self, fn, item: dict, detail: str) -> None:
        try:
            fn(item, detail)
        except Exception:
            log.exception("outbox callback failed for item %s", item["id"])
