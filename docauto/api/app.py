"""REST API for the Document Automation component (base path /api/docs/v1).

Run:  uvicorn docauto.api.app:app --host 0.0.0.0 --port 8085
"""
from __future__ import annotations

import hmac
import io
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .. import tenants
from ..config import load_config
from ..errors import DocAutoError, InvalidInput, PolicyDenied, ProviderUnavailable, UnsupportedCapability
from ..models import FieldSchema
from ..pipeline import DocumentService, ProcessOptions

STATIC = Path(__file__).parent / "static"
API = "/api/docs/v1"

app = FastAPI(title="Oneoffice Document Automation", version="1.0.0",
              description="Pluggable OCR + IDP component: open-source, licensed/cloud and AI engine options.")
app.add_middleware(tenants.TenantMiddleware)
_services: dict[str, DocumentService] = {}   # one per tenant, each with its own data folder and routing
_services_lock = threading.RLock()


def service() -> DocumentService:
    """The DocumentService of the request's tenant (see docauto.tenants)."""
    return _service_for(tenants.current.get())


def _service_for(tenant: str) -> DocumentService:
    svc = _services.get(tenant)
    if svc is None:
        with _services_lock:
            svc = _services.get(tenant)
            if svc is None:
                svc = _services[tenant] = _build_service(tenant)
    return svc


def _tenant_config(tenant: str, data_dir: str, options_dir: Optional[str]) -> dict:
    env = {**os.environ, **tenants.read_overrides(_base_data_dir(), tenant)}
    cfg = load_config(os.environ.get("DOCAUTO_CONFIG"), env=env)
    cfg["storage"]["data_dir"] = data_dir
    if options_dir:
        cfg["options_dir"] = options_dir
    return cfg


def _build_service(tenant: str) -> DocumentService:
    if tenant == tenants.DEFAULT:
        return DocumentService(config_path=os.environ.get("DOCAUTO_CONFIG"))
    base = _service_for(tenants.DEFAULT).config
    return DocumentService(_tenant_config(tenant, str(tenants.data_dir(base["storage"]["data_dir"], tenant)),
                                          base.get("options_dir")))


def _base_data_dir() -> str:
    return _service_for(tenants.DEFAULT).config["storage"]["data_dir"]


def set_service(svc: DocumentService) -> None:
    """Used by tests and by hosts embedding the component in-process: becomes the default tenant's service
    (other tenants' data then lives under its data folder)."""
    with _services_lock:
        for t in list(_services):
            _drop(t)
        _services[tenants.DEFAULT] = svc


def auth(authorization: Optional[str] = Header(default=None)) -> str:
    """The service token (DOCAUTO_API_TOKEN) the Command Center sends; returns the request's tenant.
    Without a token configured the API is open (local testing only)."""
    token = os.environ.get("DOCAUTO_API_TOKEN")
    if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(401, "invalid token")
    return tenants.current.get()


@app.exception_handler(DocAutoError)
def _err(_, e: DocAutoError):
    status = {InvalidInput: 400, PolicyDenied: 403, UnsupportedCapability: 422, ProviderUnavailable: 503}
    code = next((s for t, s in status.items() if isinstance(e, t)), 502)
    return JSONResponse(e.to_dict(), status_code=code)


# ------------------------------------------------------------------ health
@app.get("/health")
def health():
    svc = service()
    return {"status": "ok", "profile": svc.policy.profile,
            "enginesAvailable": [p.info.id for p in svc.registry.available()]}


# ------------------------------------------------------------------ documents
@app.post(f"{API}/documents", status_code=202)
async def upload_document(file: UploadFile = File(...), options: str = Form("{}"), wait: bool = Query(False),
                          tenant: str = Depends(auth)):
    """Upload + process. `options` is ProcessOptions JSON. wait=true processes synchronously."""
    try:
        opt = ProcessOptions.model_validate({**json.loads(options or "{}"), "tenantId": tenant})
    except Exception as e:
        raise HTTPException(400, f"invalid options: {e}")
    data = await file.read()
    ids = service().submit(data, file.filename or "document", opt, wait=wait)
    job = service().store.get_job(ids["jobId"])
    return {**ids, "status": job["status"], "error": job["error"]}


@app.get(f"{API}/documents")
def list_documents(limit: int = 50, _: str = Depends(auth)):
    return service().store.list_documents(limit)


@app.get(f"{API}/jobs/{{job_id}}")
def get_job(job_id: str, _: str = Depends(auth)):
    job = service().store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.get(f"{API}/documents/{{doc_id}}/result")
def get_result(doc_id: str, _: str = Depends(auth)):
    res = service().store.get_result(doc_id)
    if not res:
        raise HTTPException(404, "result not available (unknown document or still processing)")
    return json.loads(res.model_dump_json())


@app.get(f"{API}/documents/{{doc_id}}/fields")
def get_fields(doc_id: str, _: str = Depends(auth)):
    """Flat {name: value} dictionary: the simplest thing for a bot to consume."""
    res = service().store.get_result(doc_id)
    if not res:
        raise HTTPException(404, "result not available")
    return {"documentId": doc_id, "reviewRequired": res.review.required, "reviewStatus": res.review.status,
            "fields": {f.name: f.value for f in res.fields},
            "confidence": {f.name: f.confidence for f in res.fields}}


@app.get(f"{API}/documents/{{doc_id}}/file")
def get_file(doc_id: str, _: str = Depends(auth)):
    f = service().store.file_bytes(doc_id)
    if not f:
        raise HTTPException(404, "not found")
    data, name, mime = f
    return Response(data, media_type=mime, headers={"Content-Disposition": f'inline; filename="{name}"'})


@app.get(f"{API}/documents/{{doc_id}}/page/{{page}}.png")
def get_page_image(doc_id: str, page: int, _: str = Depends(auth)):
    from ..preprocess import ingest
    f = service().store.file_bytes(doc_id)
    if not f:
        raise HTTPException(404, "not found")
    ing = ingest(f[0], f[1], pages=[page], do_deskew=True)  # same geometry as OCR
    if not ing.images:
        raise HTTPException(404, "page not found")
    buf = io.BytesIO()
    img = ing.images[0]
    img.thumbnail((1400, 1800))
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


@app.get(f"{API}/documents/{{doc_id}}/export")
def export(doc_id: str, format: str = Query("csv", pattern="^(csv|json|xlsx)$"), _: str = Depends(auth)):
    res = service().store.get_result(doc_id)
    if not res:
        raise HTTPException(404, "result not available")
    if format == "json":
        return json.loads(res.model_dump_json())
    rows = [("field", "value", "confidence", "page", "corrected")] + \
           [(f.name, json.dumps(f.value, ensure_ascii=False) if isinstance(f.value, (list, dict)) else f.value,
             f.confidence, f.page, f.corrected) for f in res.fields]
    if format == "xlsx":
        try:
            from openpyxl import Workbook
        except ImportError:
            raise HTTPException(501, "openpyxl not installed")
        wb = Workbook()
        ws = wb.active
        ws.title = "fields"
        for r in rows:
            ws.append(list(r))
        for p in res.pages:
            for ti, t in enumerate(p.tables, 1):
                tws = wb.create_sheet(f"p{p.pageNumber}_table{ti}"[:31])
                for row in t.to_matrix():
                    tws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return Response(buf.getvalue(),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{doc_id}.xlsx"'})
    import csv
    s = io.StringIO()
    csv.writer(s).writerows(rows)
    return Response(s.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{doc_id}.csv"'})


# ------------------------------------------------------------------ synchronous utilities
@app.post(f"{API}/ocr/screen")
async def screen_ocr(file: UploadFile = File(...), provider: str = Form("auto"), languages: str = Form("en"),
                     _: str = Depends(auth)):
    res = service().screen_ocr(await file.read(), provider, [l.strip() for l in languages.split(",") if l.strip()])
    return {"text": res.text, "lines": [l.model_dump() for p in res.pages for l in p.lines],
            "engine": res.provenance.steps[-1].providerId}


@app.post(f"{API}/pdf/searchable")
async def searchable_pdf(file: UploadFile = File(...), languages: str = Form("en"), provider: str = Form("auto"),
                         _: str = Depends(auth)):
    pdf = service().searchable_pdf(await file.read(), file.filename or "doc",
                                   [l.strip() for l in languages.split(",")], provider)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="searchable.pdf"'})


# ------------------------------------------------------------------ schemas
@app.get(f"{API}/schemas")
def list_schemas(_: str = Depends(auth)):
    return service().store.list_schemas()


@app.post(f"{API}/schemas")
def save_schema(schema: FieldSchema, _: str = Depends(auth)):
    return service().store.save_schema(schema)


@app.get(f"{API}/schemas/{{schema_id}}")
def get_schema(schema_id: str, _: str = Depends(auth)):
    s = service().store.get_schema(schema_id)
    if not s:
        raise HTTPException(404, "schema not found")
    return s


# ------------------------------------------------------------------ engines / admin
@app.get(f"{API}/providers")
def providers(_: str = Depends(auth)):
    svc = service()
    return {"deploymentProfile": svc.policy.profile,
            "optionPacks": svc.config.get("option_packs", {}),
            "providers": svc.providers()}


# ------------------------------------------------------------------ validation station
class ReviewSubmit(BaseModel):
    reviewer: str
    fields: dict[str, Any] = {}
    reject: bool = False
    classification: Optional[str] = None


@app.get(f"{API}/reviews")
def reviews(status: str = "pending", _: str = Depends(auth)):
    return service().store.reviews(status)


@app.post(f"{API}/reviews/{{doc_id}}/complete")
def complete_review(doc_id: str, body: ReviewSubmit, _: str = Depends(auth)):
    res = service().complete_review(doc_id, body.fields, body.reviewer, body.reject, body.classification)
    return {"documentId": doc_id, "reviewStatus": res.review.status,
            "fields": {f.name: f.value for f in res.fields}}


@app.get("/validation")
def validation_station():
    return FileResponse(STATIC / "validation.html")


# ------------------------------------------------------------------ usage
@app.get(f"{API}/usage")
def usage(groupBy: str = "provider_id", _: str = Depends(auth)):
    try:
        return service().store.usage_summary(groupBy)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ admin (used by the web UI)
from fastapi.responses import RedirectResponse  # noqa: E402

from .. import settings as settings_mod  # noqa: E402


def reload_service() -> DocumentService:
    """Rebuild the request tenant's service after its settings change (keeps the same data folder). Server-wide
    settings (the default tenant's) reach every tenant, so the other tenants are rebuilt too."""
    tenant = tenants.current.get()
    old = service()
    cfg = _tenant_config(tenant, old.config["storage"]["data_dir"], old.config.get("options_dir"))
    with _services_lock:
        if tenant == tenants.DEFAULT:
            for t in [t for t in _services if t != tenants.DEFAULT]:
                _drop(t)
        _drop(tenant)
        _services[tenant] = new = DocumentService(cfg)
    _start_docflow()
    return new


@app.get(f"{API}/admin/settings")
def get_settings(tenant: str = Depends(auth)):
    out = settings_mod.describe()
    if tenant != tenants.DEFAULT:  # the tenant's own routing over the server's; keys and profile are server-wide
        own = tenants.read_overrides(_base_data_dir(), tenant)
        out["defaults"] = [{**f, "value": own.get(f["key"], f["value"]), "tenant": f["key"] in own}
                           for f in out["defaults"]]
        out["tenant"] = tenant
        out.pop("envFile", None)
    return out


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


@app.put(f"{API}/admin/settings")
def put_settings(body: SettingsUpdate, tenant: str = Depends(auth)):
    try:
        changed = (settings_mod.apply(body.values) if tenant == tenants.DEFAULT
                   else tenants.write_overrides(_base_data_dir(), tenant, body.values))
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        svc = reload_service() if changed else service()
    except Exception as e:  # bad value (e.g. unknown profile): report, keep running old service
        raise HTTPException(400, f"settings saved but service could not reload: {e}")
    return {"changed": changed, "profile": svc.policy.profile, "providers": svc.providers()}


@app.get(f"{API}/costs")
def costs(pages: int = 10000, cloudShare: float = 0.2, extractShare: float = 0.3, cloudOcr: str = "azure-di",
          extraction: str = "azure-di", model: Optional[str] = None, optIn: Optional[bool] = None,
          _: str = Depends(auth)):
    """List price per 1,000 pages for every engine, and a monthly estimate (defaults: the plan's example).
    optIn=true projects the cost as if the client had opted in (the UI shows that next to today's $0)."""
    from .. import costs as costs_mod
    svc = service()
    configured = costs_mod.claude_model(svc.config)
    opted_in = svc.policy.cloud_opt_in if optIn is None else optIn
    return {"prices": costs_mod.price_table(svc.config, [p.info.id for p in svc.registry.providers.values()]),
            "claudeModel": configured, "cloudOptIn": svc.policy.cloud_opt_in,
            "estimate": costs_mod.estimate(pages, cloudShare, extractShare, cloudOcr or None, extraction,
                                           model or configured, opted_in)}


@app.get(f"{API}/admin/version")
def get_version():
    from .. import updates
    return updates.version_info()


@app.post(f"{API}/admin/update")
def post_update(_: str = Depends(auth)):
    from .. import updates
    try:
        updates.start_update()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"started": True}


@app.get(f"{API}/admin/installer.bat")
def get_installer(_: str = Depends(auth)):
    from fastapi.responses import Response
    from .. import updates
    v = updates.version_info()
    return Response(updates.installer_bat(v["repo"], v["ref"]), media_type="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="Install-OneOffice-DocFlow.bat"'})


@app.post(f"{API}/admin/providers/{{provider_id}}/test")
def test_provider(provider_id: str, _: str = Depends(auth)):
    """Run a tiny real request through one engine and report result/latency."""
    import time
    from PIL import Image, ImageDraw, ImageFont

    from ..models import BBox, DocumentResult, FieldDef, Line, Page
    from ..provider import Capability, ExtractRequest, RecognizeRequest

    svc = service()
    p = svc.registry.get(provider_id)
    if p is None:
        raise HTTPException(404, "unknown engine")
    ok, why = svc.policy.check(p.info)
    if not ok:
        return {"ok": False, "stage": "policy", "message": why}
    if not p.available:
        return {"ok": False, "stage": "configuration", "message": p.reason}
    t0 = time.time()
    try:
        if p.info.kind == "ocr":
            img = Image.new("RGB", (900, 200), "white")
            d = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("arial.ttf", 48)
            except OSError:
                try:
                    font = ImageFont.truetype("DejaVuSans.ttf", 48)
                except OSError:
                    font = ImageFont.load_default()
            d.text((40, 70), "TEST INVOICE 12345", fill="black", font=font)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            req = RecognizeRequest(document_id="engine-test", pages=[img], languages=["en"],
                                   capabilities=[Capability.OCR_PRINTED], source_bytes=buf.getvalue(),
                                   mime_type="image/png")
            if provider_id == "pdf-text":
                return {"ok": True, "message": "built-in: reads digital PDF text layers (no test needed)"}
            res = p.instance.recognize(req)
            text = res.text.strip()
            good = "12345" in text
            return {"ok": good, "stage": "recognize", "ms": int((time.time() - t0) * 1000), "output": text,
                    "message": "engine read the test image" if good else "engine responded but text looks wrong"}
        doc = DocumentResult(documentId="engine-test", pages=[Page(pageNumber=1, width=1, height=1, lines=[
            Line(text="Invoice Number: TEST-12345", bbox=BBox(x=0.1, y=0.1, w=0.5, h=0.05), confidence=0.99)])])
        doc.rebuild_text()
        schema = FieldSchema(name="Engine test", fields=[FieldDef(name="invoice_number", label="Invoice Number")])
        fields = p.instance.extract(ExtractRequest(document=doc, schema=schema))
        val = fields[0].value if fields else None
        return {"ok": val == "TEST-12345", "stage": "extract", "ms": int((time.time() - t0) * 1000),
                "output": str(val), "message": "engine extracted the test field" if val == "TEST-12345"
                else "engine responded but the value looks wrong"}
    except DocAutoError as e:
        return {"ok": False, "stage": "call", "message": f"{e.code}: {e.message}"}
    except Exception as e:
        return {"ok": False, "stage": "call", "message": f"{type(e).__name__}: {e}"}


@app.get("/ui")
def ui():
    return FileResponse(STATIC / "app.html", headers={"Cache-Control": "no-cache"})


_BRAND = {"favicon.png": "image/png", "ooa-logo.png": "image/png",
          "ooa-logo.svg": "image/svg+xml", "ooa-mark.svg": "image/svg+xml"}


@app.get("/ui/brand/{name}")
def ui_brand(name: str):
    if name not in _BRAND:
        raise HTTPException(404, "not found")
    return FileResponse(STATIC / "brand" / name, media_type=_BRAND[name],
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC / "brand" / "favicon.png", media_type="image/png")


@app.get("/ui/docflow.js")
def ui_docflow_js():
    return FileResponse(STATIC / "docflow.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/")
def root():
    return RedirectResponse("/ui")


# ================================================================== Oneoffice DocFlow
from contextlib import contextmanager  # noqa: E402

from ..docflow import INBOX_ID, DocFlowService, DocType, Flow, library, parse_table_file  # noqa: E402

DF = f"{API}/df"
_dfs: dict[str, DocFlowService] = {}


def docflow() -> DocFlowService:
    """The request tenant's DocFlow, bound to its DocumentService (rebuilt after settings reloads)."""
    tenant = tenants.current.get()
    svc = _service_for(tenant)
    df = _dfs.get(tenant)
    if df is None or df.svc is not svc:
        with _services_lock:
            df = _dfs.get(tenant)
            if df is None or df.svc is not svc:
                if df is not None:
                    df.stop_watcher()
                df = _dfs[tenant] = DocFlowService(svc)
                try:
                    df.migrate_instances()
                except Exception:  # pragma: no cover - migration is best effort
                    import logging
                    logging.getLogger("docauto").exception("instance migration failed")
                if os.environ.get("DOCAUTO_FOLDER_WATCHER", "true").lower() != "false":
                    df.start_watcher()
    return df


def _drop(tenant: str) -> None:
    """Forget a tenant's services (caller holds the lock); its DocFlow folder watcher stops."""
    _services.pop(tenant, None)
    df = _dfs.pop(tenant, None)
    if df is not None:
        df.stop_watcher()


@app.on_event("startup")
def _start_docflow():
    """DocFlow, with its folder watcher, for every tenant that has data."""
    for tenant in [tenants.DEFAULT, *tenants.known(_base_data_dir())]:
        token = tenants.current.set(tenant)
        try:
            docflow()
        finally:
            tenants.current.reset(token)


# ---- doc types
@app.get(f"{DF}/library")
def df_library(_: str = Depends(auth)):
    return library()


@app.get(f"{DF}/doctypes")
def df_doctypes(_: str = Depends(auth)):
    d = docflow()
    used = {}
    for f in d.flows():
        for t in (d.flow_doctypes(f) if f.isInbox else [d.doctype(x) for x in f.docTypes]):
            used.setdefault(t.id, []).append(f.name)
    return [{**t.model_dump(), "usedBy": used.get(t.id, []),
             "learnedCount": sum(len(v) for v in t.learning.aliases.values())} for t in d.doctypes()]


@app.post(f"{DF}/doctypes")
def df_create_doctype(body: DocType, _: str = Depends(auth)):
    return docflow().save_doctype(body, new=True)


@app.get(f"{DF}/doctypes/{{dt_id}}")
def df_get_doctype(dt_id: str, _: str = Depends(auth)):
    return docflow().doctype(dt_id)


@app.put(f"{DF}/doctypes/{{dt_id}}")
def df_update_doctype(dt_id: str, body: dict[str, Any], _: str = Depends(auth)):
    try:
        return docflow().update_doctype(dt_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete(f"{DF}/doctypes/{{dt_id}}")
def df_delete_doctype(dt_id: str, _: str = Depends(auth)):
    docflow().delete_doctype(dt_id)
    return {"deleted": dt_id}


@app.post(f"{DF}/doctypes/suggest")
async def df_suggest(files: list[UploadFile] = File(...), _: str = Depends(auth)):
    """Teach from samples: propose a document type from 1-3 example documents."""
    return docflow().suggest([(await f.read(), f.filename or "sample") for f in files])


@app.get(f"{DF}/doctypes/{{dt_id}}/learning")
def df_learning(dt_id: str, _: str = Depends(auth)):
    dt = docflow().doctype(dt_id)
    return {"enabled": dt.learning.enabled, "aliases": dt.learning.aliases,
            "corrections": service().store.corrections(dt_id, 100)}


class _Forget(BaseModel):
    field: str
    alias: str


@app.post(f"{DF}/doctypes/{{dt_id}}/learning/forget")
def df_forget(dt_id: str, body: _Forget, _: str = Depends(auth)):
    return docflow().forget(dt_id, body.field, body.alias)


# ---- flows
@app.get(f"{DF}/flows")
def df_flows(_: str = Depends(auth)):
    d = docflow()
    return [{**f.model_dump(), "stats": service().store.instance_stats(f.id),
             "docTypeNames": [t.name for t in d.flow_doctypes(f)]} for f in d.flows()]


@app.post(f"{DF}/flows")
def df_create_flow(body: Flow, _: str = Depends(auth)):
    body.isInbox = False
    return docflow().save_flow(body, new=True)


@app.get(f"{DF}/flows/{{flow_id}}")
def df_get_flow(flow_id: str, _: str = Depends(auth)):
    return docflow().flow(flow_id)


@app.put(f"{DF}/flows/{{flow_id}}")
def df_update_flow(flow_id: str, body: dict[str, Any], _: str = Depends(auth)):
    try:
        return docflow().update_flow(flow_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete(f"{DF}/flows/{{flow_id}}")
def df_delete_flow(flow_id: str, _: str = Depends(auth)):
    docflow().delete_flow(flow_id)
    return {"deleted": flow_id}


@app.post(f"{DF}/flows/{{flow_id}}/live")
def df_go_live(flow_id: str, live: bool = Query(True), _: str = Depends(auth)):
    return docflow().update_flow(flow_id, {"status": "live" if live else "draft"})


@app.get(f"{DF}/flows/{{flow_id}}/stats")
def df_flow_stats(flow_id: str, _: str = Depends(auth)):
    docflow().flow(flow_id)
    return service().store.instance_stats(flow_id)


@app.post(f"{DF}/flows/{{flow_id}}/documents", status_code=202)
async def df_upload(flow_id: str, files: list[UploadFile] = File(...), wait: bool = Query(False),
                    source: str = Query("upload"), requireLive: bool = Query(False), _: str = Depends(auth)):
    out = []
    for f in files:
        out.append({"fileName": f.filename, **docflow().upload(flow_id, await f.read(), f.filename or "document",
                                                               source=source, wait=wait, require_live=requireLive)})
    return {"documents": out}


@app.get(f"{DF}/flows/{{flow_id}}/documents")
def df_flow_documents(flow_id: str, status: Optional[str] = None, limit: int = 200, offset: int = 0,
                      fields: bool = False, _: str = Depends(auth)):
    """fields=true adds each document's extracted values, confidence, pages and engines (the platform's lists)."""
    sts = [s for s in (status or "").split(",") if s] or None
    rows = service().store.instance_documents(flow_id if flow_id != "all" else None, sts, limit, offset)
    if fields:
        store = service().store
        for r in rows:
            res = store.get_result(r["id"])
            r["fields"] = {f.name: f.value for f in res.fields} if res else {}
            r["confidence"] = res.confidence if res else None
            r["pages"] = len(res.pages) if res else None
            r["engines"] = list(dict.fromkeys(s.providerId for s in res.provenance.steps
                                              if s.stage == "digitize")) if res else []
    return rows


@app.get(f"{DF}/flows/{{flow_id}}/export")
def df_export(flow_id: str, format: str = Query("csv", pattern="^(csv|xlsx)$"), status: str = "success,validated",
              _: str = Depends(auth)):
    data, mime, name = docflow().export(flow_id, format, [s for s in status.split(",") if s])
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get(f"{DF}/flows/{{flow_id}}/deliveries")
def df_delivery_log(flow_id: str, _: str = Depends(auth)):
    return service().store.delivery_list(flow_id if flow_id != "all" else None, 200)


@app.post(f"{DF}/flows/{{flow_id}}/deliveries/{{delivery_id}}/test")
def df_test_delivery(flow_id: str, delivery_id: str, _: str = Depends(auth)):
    return docflow().test_delivery(flow_id, delivery_id)


@app.post(f"{DF}/flows/{{flow_id}}/intake/scan")
def df_scan(flow_id: str, _: str = Depends(auth)):
    fl = docflow().flow(flow_id)
    if not fl.intake.inputFolder:
        raise HTTPException(400, "no input folder configured")
    if not Path(fl.intake.inputFolder).is_dir():
        raise HTTPException(400, f"input folder not found: {fl.intake.inputFolder}")
    return {"ingested": docflow().scan_folder(fl, require_live=False)}


# ---- documents & review desk
@app.get(f"{DF}/documents/{{doc_id}}")
def df_document(doc_id: str, _: str = Depends(auth)):
    return docflow().document(doc_id)


class _Review(BaseModel):
    reviewer: str = "reviewer"
    fields: dict[str, Any] = {}
    action: str = "submit"


@app.post(f"{DF}/documents/{{doc_id}}/review")
def df_review(doc_id: str, body: _Review, _: str = Depends(auth)):
    if body.action not in ("submit", "invalid"):
        raise HTTPException(400, "action must be 'submit' or 'invalid'")
    reviewer = tenants.current_user.get() or body.reviewer  # the platform's signed-in user, when it sends one
    return docflow().review(doc_id, body.fields, reviewer, body.action)


class _Reclassify(BaseModel):
    docType: str


@app.post(f"{DF}/documents/{{doc_id}}/reclassify")
def df_reclassify(doc_id: str, body: _Reclassify, _: str = Depends(auth)):
    return docflow().reclassify(doc_id, body.docType)


@app.get(f"{DF}/review")
def df_review_queue(flow: Optional[str] = None, _: str = Depends(auth)):
    return docflow().review_queue(flow)


@app.get(f"{DF}/review/next")
def df_review_next(flow: Optional[str] = None, after: Optional[str] = None, _: str = Depends(auth)):
    return {"documentId": service().store.next_for_validation(flow if flow and flow != "all" else None, after)}


@app.get(f"{DF}/overview")
def df_overview(_: str = Depends(auth)):
    return docflow().overview()


# ---- reference data
@app.get(f"{DF}/reftables")
def df_reftables(_: str = Depends(auth)):
    return service().store.ref_list()


@app.post(f"{DF}/reftables")
async def df_ref_upload(file: UploadFile = File(...), name: str = Form(""), _: str = Depends(auth)):
    data = await file.read()
    cols, rows = parse_table_file(data, file.filename or "table.csv")
    title = name.strip() or Path(file.filename or "table").stem
    ref_id = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "table"
    service().store.ref_save(ref_id, title, cols, rows)
    return {"id": ref_id, "name": title, "columns": cols, "rowCount": len(rows)}


@app.get(f"{DF}/reftables/{{ref_id}}")
def df_ref_get(ref_id: str, limit: int = 100, _: str = Depends(auth)):
    t = service().store.ref_get(ref_id)
    if not t:
        raise HTTPException(404, "not found")
    return {**t, "rowCount": len(t["rows"]), "rows": t["rows"][:limit]}


@app.delete(f"{DF}/reftables/{{ref_id}}")
def df_ref_delete(ref_id: str, _: str = Depends(auth)):
    service().store.ref_delete(ref_id)
    return {"deleted": ref_id}
