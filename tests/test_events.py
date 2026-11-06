"""Document events to the OOA Command Center (POST /api/docflow-callback/events), with a mocked HTTP layer."""
import time

import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLES, make_config
from docauto import events, outbox

DF = "/api/docs/v1/df"
PDF = (SAMPLES / "invoice_digital.pdf").read_bytes()
# the sample's supplier is the unlabelled company line at the top, read at <= 75% so a person checks it; these
# processes accept it, so the invoice goes straight through
AUTO = {"fieldThresholds": {"supplier_name": 0.7}}


class Resp:
    def __init__(self, code):
        self.status_code = code


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", 0.01)
    monkeypatch.setenv("DOCAUTO_PLATFORM_URL", "https://cc.example/{tenant}/")
    monkeypatch.setenv("DOCAUTO_API_TOKEN", "svc-token")


def test_post_with_the_start_bot_auth_and_retries(fast, monkeypatch):
    sent, codes = [], [503, 500, 200]
    monkeypatch.setattr(events.httpx, "post", lambda url, json=None, headers=None, timeout=None:
                        sent.append((url, json, headers)) or Resp(codes.pop(0)))
    body = events.body("processed", "d1", "ap", "AP invoices", "success", "a.pdf", "invoice",
                       {"total": 11500.0, "line_items": [{"amount": 1}], "currency": "SAR"})
    events.emit("acme co", body).join(5)
    assert len(sent) == 3
    url, payload, headers = sent[-1]
    assert url == "https://cc.example/acme co/api/docflow-callback/events"
    assert headers["Authorization"] == "Bearer svc-token" and headers["X-OOA-Tenant"] == "acme%20co"
    assert payload["fields"] == {"total": 11500.0, "currency": "SAR"}                 # scalar fields only
    assert set(payload) == {"event", "event_id", "document_id", "process_id", "process", "status", "file_name",
                            "doc_type", "fields", "at"}
    assert payload["at"].endswith("Z") and payload["doc_type"] == "invoice"
    assert len({p["event_id"] for _, p, _ in sent}) == 1                       # retries carry the same event_id
    assert payload["event_id"] != events.body("processed", "d1", "ap", "A", "success", "", None, {})["event_id"]


def test_refused_events_are_not_retried(fast, monkeypatch):
    sent = []
    monkeypatch.setattr(events.httpx, "post", lambda *a, **k: sent.append(1) or Resp(401))
    events.emit("default", events.body("approved", "d", "p", "P", "validated", "a", None, {})).join(5)
    assert sent == [1]


def test_nothing_is_sent_without_a_command_center(monkeypatch):
    monkeypatch.delenv("DOCAUTO_PLATFORM_URL", raising=False)
    monkeypatch.setattr(events.httpx, "post", lambda *a, **k: pytest.fail("posted"))
    assert events.emit("default", events.body("processed", "d", "p", "P", "success", "a", None, {})) is None
    with pytest.raises(ValueError):
        events.body("exploded", "d", "p", "P", "x", "a", None, {})


# ------------------------------------------------------------------ DocFlow emits them
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DOCAUTO_FOLDER_WATCHER", "false")
    monkeypatch.delenv("DOCAUTO_API_TOKEN", raising=False)
    import docauto.api.app as appmod
    from docauto.pipeline import DocumentService
    appmod.set_service(DocumentService(make_config()))
    return TestClient(appmod.app)


@pytest.fixture
def emitted(monkeypatch):
    """What the outbox sender posts to the Command Center (one successful attempt each)."""
    got = []
    monkeypatch.setenv("DOCAUTO_PLATFORM_URL", "https://cc.example/{tenant}")
    monkeypatch.setattr(events, "send", lambda tenant, payload: got.append((tenant, payload)) or (True, "HTTP 200", False))
    return got


def up(client, fid):
    r = client.post(f"{DF}/flows/{fid}/documents", params={"wait": "true"},
                    files=[("files", ("inv.pdf", PDF, "application/pdf"))])
    return r.json()["documents"][0]["documentId"]


def wait_for(got, event, timeout=8, count=1):
    """The events of this kind once at least `count` have arrived (documents are read in parallel)."""
    end = time.time() + timeout
    while time.time() < end:
        hit = [p for _, p in got if p["event"] == event]
        if len(hit) >= count:
            return hit
        time.sleep(0.05)
    raise AssertionError(f"not {count} {event} event(s) in {[p['event'] for _, p in got]}")


def test_processed_review_and_decisions(client, emitted):
    fid = client.post(f"{DF}/flows", json={"name": "AP invoices", "docTypes": ["invoice"], "review": AUTO}).json()["id"]
    d1 = up(client, fid)
    ev = wait_for(emitted, "processed")[0]
    assert ev["document_id"] == d1 and ev["process_id"] == fid and ev["process"] == "AP invoices"
    assert ev["status"] == "success" and ev["doc_type"] == "invoice" and ev["file_name"] == "inv.pdf"
    assert ev["fields"]["invoice_number"] == "INV-2026-0042" and "line_items" not in ev["fields"]
    assert emitted[0][0] == "default"

    strict = client.post(f"{DF}/flows", json={"name": "Strict", "docTypes": ["invoice"],
                                              "review": {"fieldThresholds": {"total": 0.999}}}).json()["id"]
    d2, d3 = up(client, strict), up(client, strict)
    assert {p["document_id"] for p in wait_for(emitted, "needs_review", count=2)} == {d2, d3}
    client.post(f"{DF}/documents/{d2}/review", json={"reviewer": "qa", "fields": {"total": 11500.0}})
    client.post(f"{DF}/documents/{d3}/review", json={"reviewer": "qa", "action": "invalid"})
    assert wait_for(emitted, "approved")[0]["document_id"] == d2 and wait_for(emitted, "approved")[0]["status"] == "validated"
    assert wait_for(emitted, "rejected")[0]["document_id"] == d3 and wait_for(emitted, "rejected")[0]["status"] == "invalid"


def test_delivery_failed_and_review_overdue(client, emitted, monkeypatch):
    import docauto.api.app as appmod
    import docauto.docflow as dfm

    def down(*a, **k):
        raise dfm.httpx.ConnectError("refused")
    monkeypatch.setattr(dfm.httpx, "post", down)
    monkeypatch.setattr(outbox, "BACKOFF", 0.01)          # retries with backoff, then delivery_failed
    fid = client.post(f"{DF}/flows", json={
        "name": "Slow", "docTypes": ["invoice"], "review": {"fieldThresholds": {"total": 0.999}, "slaHours": 0},
        "deliveries": [{"type": "webhook", "name": "ERP", "on": ["validation"], "params": {"url": "http://erp.local/hook"}}]}).json()["id"]
    doc = up(client, fid)
    failed = wait_for(emitted, "delivery_failed")[0]
    assert failed["document_id"] == doc and failed["status"] == "validation"
    time.sleep(0.05)
    assert appmod.docflow().check_sla() == 1
    overdue = wait_for(emitted, "review_overdue")[0]
    assert overdue["document_id"] == doc and overdue["process"] == "Slow"


def test_outbox_sends_each_event_once_and_survives_a_restart(client, monkeypatch):
    """Events wait in the outbox until the Command Center takes them; a delivered one is never sent again."""
    import docauto.api.app as appmod
    monkeypatch.setenv("DOCAUTO_PLATFORM_URL", "https://cc.example/{tenant}")
    monkeypatch.setattr(outbox, "BACKOFF", 0.01)
    up_now = {"ok": False}
    got = []

    def send(tenant, payload):
        got.append(payload)
        return (True, "HTTP 200", False) if up_now["ok"] else (False, "ConnectError: refused", True)
    monkeypatch.setattr(events, "send", send)
    df = appmod.docflow()
    df.outbox.stop()                                   # the service "stops" with the event still queued
    fid = client.post(f"{DF}/flows", json={"name": "AP", "docTypes": ["invoice"], "review": AUTO}).json()["id"]
    up(client, fid)
    pending = df.store.outbox_list("pending")
    assert [p["name"] for p in pending] == ["processed"]
    restarted = outbox.Sender(df.store, {"event": df._send_event_item})   # a new process picks it up
    restarted.run_once()
    assert df.store.outbox_list("pending")[0]["attempts"] == 1           # failed once, retried later
    up_now["ok"] = True
    time.sleep(0.05)
    restarted.run_once()
    assert df.store.outbox_list("sent")[0]["name"] == "processed"
    for _ in range(3):
        restarted.run_once()
    ids = [p["event_id"] for p in got]
    assert len(ids) == 2 and len(set(ids)) == 1                            # one event, sent once successfully
    assert client.get(f"{DF}/outbox", params={"status": "sent"}).json()[0]["kind"] == "event"


def test_status_change_and_its_event_commit_together(client, emitted, monkeypatch):
    """A listener's outbox item is written in the transaction of the status change: a failure rolls both back."""
    import docauto.api.app as appmod
    svc = appmod.service()
    fid = client.post(f"{DF}/flows", json={"name": "AP", "docTypes": ["invoice"], "review": AUTO}).json()["id"]
    doc = up(client, fid)
    before = len(svc.store.outbox_list())
    with pytest.raises(RuntimeError):
        with svc.store.tx():
            svc.store.set_doc_status(doc, "validated")
            appmod.docflow()._event("approved", doc)
            raise RuntimeError("crash before commit")
    assert len(svc.store.outbox_list()) == before and svc.store.doc_row(doc)["status"] == "success"


def test_deliveries_retry_with_backoff_and_are_logged_once(client, monkeypatch):
    import docauto.docflow as dfm
    monkeypatch.setattr(outbox, "BACKOFF", 0.01)
    codes, posted = [503, 500, 200], []
    monkeypatch.setattr(dfm.httpx, "post", lambda url, json=None, headers=None, timeout=None:
                        posted.append(json) or Resp(codes.pop(0) if codes else 200))
    fid = client.post(f"{DF}/flows", json={"name": "ERP", "docTypes": ["invoice"], "review": AUTO, "deliveries": [
        {"type": "webhook", "name": "ERP", "params": {"url": "http://erp.local/hook"}}]}).json()["id"]
    doc = up(client, fid)
    end = time.time() + 10
    while time.time() < end and not client.get(f"{DF}/flows/{fid}/deliveries").json():
        time.sleep(0.05)
    log = client.get(f"{DF}/flows/{fid}/deliveries").json()
    assert [(x["delivery"], x["ok"], x["document_id"]) for x in log] == [("ERP", 1, doc)]
    assert len(posted) == 3 and all(p == posted[0] for p in posted)          # the same body on every attempt
    sent = client.get(f"{DF}/outbox", params={"status": "sent"}).json()
    assert sent[0]["kind"] == "delivery" and sent[0]["attempts"] == 3
    time.sleep(0.2)
    assert len(posted) == 3                                                  # never sent again


def test_a_refused_delivery_fails_without_retrying(client, emitted, monkeypatch):
    import docauto.docflow as dfm
    posted = []
    monkeypatch.setattr(dfm.httpx, "post", lambda *a, **k: posted.append(1) or Resp(400))
    fid = client.post(f"{DF}/flows", json={"name": "ERP", "docTypes": ["invoice"], "review": AUTO, "deliveries": [
        {"type": "webhook", "name": "ERP", "params": {"url": "http://erp.local/hook"}}]}).json()["id"]
    up(client, fid)
    assert wait_for(emitted, "delivery_failed")[0]["status"] == "success"
    assert posted == [1]
    failed = client.get(f"{DF}/outbox", params={"status": "failed"}).json()
    assert failed[0]["last_error"] == "HTTP 400"
    assert client.get(f"{DF}/flows/{fid}/deliveries").json()[0]["detail"] == "HTTP 400 (after 1 attempt)"
