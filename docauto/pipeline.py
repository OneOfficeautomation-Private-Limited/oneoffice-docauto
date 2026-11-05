"""DocumentService: the orchestrator the RPA tool talks to (in-process or over REST)."""
from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import barcodes, einvoice, pii
from .builtin import PDF_TEXT_INFO, PdfTextProvider
from .config import load_config
from .errors import DocAutoError, InvalidInput, UnsupportedCapability
from .extraction import RULES_INFO, RulesExtractor
from .grounding import ground_document
from .models import Classification, DocumentResult, ExtractedField, FieldSchema, Page, ProvenanceStep, ReviewStatus, Source
from .normalize import detect_direction, detect_language
from .policy import PolicyEngine, _as_list
from .preprocess import ingest
from .provider import Capability, ExtractRequest, RecognizeRequest
from .registry import ProviderRegistry, RegisteredProvider
from .router import Router
from .scoring import score
from .store import Store, worker_id

log = logging.getLogger("docauto.pipeline")
# durable jobs: workers claim queued jobs; a running job whose worker sends no heartbeat for JOB_STALE_SECONDS
# is taken over by another worker (or by this service after a restart), at most JOB_MAX_ATTEMPTS times
JOB_POLL_MIN, JOB_POLL_MAX = 0.2, 2.0
JOB_HEARTBEAT_SECONDS = 15.0
JOB_STALE_SECONDS = float(os.environ.get("DOCAUTO_JOB_STALE_SECONDS") or 90)
JOB_MAX_ATTEMPTS = 3
_services: "weakref.WeakSet[DocumentService]" = weakref.WeakSet()


def close_all() -> None:
    """Stop the job workers of every service in this process (shutdown, tests)."""
    for s in list(_services):
        s.close()


def _page_confidence(page) -> Optional[float]:
    vals = [l.confidence for l in page.lines if l.confidence is not None]
    return sum(vals) / len(vals) if vals else None


def _mean_confidence(fields: list[ExtractedField]) -> float:
    vals = [f.confidence or 0.0 for f in fields if f.value not in (None, "", [])]
    return sum(vals) / len(vals) if vals else 0.0


def _same(a: Any, b: Any) -> bool:
    """Compare an extracted value with a reviewer value (numbers typed as text count as equal)."""
    if a == b:
        return True
    try:
        return float(str(a).replace(",", "")) == float(str(b).replace(",", ""))
    except (TypeError, ValueError):
        return str(a or "").strip() == str(b or "").strip()


class ProcessOptions(BaseModel):
    """What a bot asks for. Everything is optional: 'auto' lets the router decide."""
    ocrProvider: str = "auto"
    extractionProvider: str = "auto"
    languages: list[str] = Field(default_factory=lambda: ["en"])
    capabilities: list[Capability] = Field(default_factory=lambda: [Capability.OCR_PRINTED])
    pages: Optional[list[int]] = None
    deskew: bool = True
    useTextLayer: bool = True             # skip OCR for digital PDFs
    schemaId: Optional[str] = None
    schema_: Optional[FieldSchema] = Field(default=None, alias="schema")
    classifyLabels: list[str] = Field(default_factory=list)
    tenantId: str = "default"
    includeRaw: bool = False
    strictProvider: bool = False          # no fallback to other engines (used by "Compare engines")
    instanceId: Optional[str] = None      # learning instance this document belongs to
    instanceVersion: Optional[int] = None
    source: str = "api"                   # api | upload | folder | bot
    fieldThreshold: Optional[float] = None
    docThreshold: Optional[float] = None
    fieldThresholds: dict[str, float] = Field(default_factory=dict)   # per field name, over fieldThreshold
    einvoiceFirst: bool = True            # e-invoice XML replaces OCR; e-invoice QR codes check the fields
    instructions: Optional[str] = None    # extra guidance for AI extractors (e.g. learned corrections)
    doctypeId: Optional[str] = None       # DocFlow document type
    parentId: Optional[str] = None        # set on documents split out of a batch
    reuseFrom: Optional[str] = None       # reuse OCR of this document instead of re-reading pages
    reusePages: Optional[list[int]] = None
    role: str = "document"                # document | batch (classify & split only)
    waitChildren: bool = False
    # PII masking before cloud AI models (docauto.pii). None = the server/tenant setting (DOCAUTO_MASK_PII, on).
    maskPii: Optional[bool] = None
    cloudImages: bool = False             # with masking on, send page images to cloud models anyway

    model_config = {"populate_by_name": True}


class DocumentService:
    def __init__(self, config: dict | None = None, config_path: str | None = None):
        self.config = config or load_config(config_path)
        st = self.config["storage"]
        self.store = Store(st["data_dir"], database_url=st.get("database_url"), schema=st.get("schema"),
                           tenant=st.get("tenant") or "default", schema_prefix=st.get("schema_prefix"),
                           key_file=st.get("key_file"))
        self.registry = ProviderRegistry(self.config)
        self.registry.register(PdfTextProvider(), PDF_TEXT_INFO)
        self.registry.register(RulesExtractor(), RULES_INFO)
        self.registry.discover()
        self.policy = PolicyEngine(self.config)
        self.router = Router(self.registry, self.policy, self.config)
        self.max_workers = int(self.config.get("workers", 4))
        self.pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="docauto-job")
        self.worker = f"{worker_id()}:{uuid.uuid4().hex[:6]}"
        self._active: set[str] = set()          # jobs this service is running (heartbeats)
        self._active_lock = threading.Lock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._dispatcher: Optional[threading.Thread] = None
        self._heart: Optional[threading.Thread] = None
        _services.add(self)
        self.mask_pii_default = str((self.config.get("privacy") or {}).get("mask_pii", True)).lower() not in (
            "false", "0", "no", "off", "none")
        hr = self.config.get("human_review", {})
        self.field_threshold = float(hr.get("field_confidence_threshold", 0.85))
        self.doc_threshold = float(hr.get("document_confidence_threshold", 0.80))
        self.listeners: list = []        # callables(event, doc_id, **info): notified after status changes
        self.post_processors: list = []  # callables(doc_id, result, opt, status) -> new status | None (rules, split)

    def _emit(self, event: str, doc_id: str, **info) -> None:
        """Listeners run inside the caller's transaction (each in a savepoint), so what they queue in the outbox
        is committed together with the status change, or not at all."""
        for fn in list(self.listeners):
            try:
                with self.store.tx():
                    fn(event, doc_id, **info)
            except Exception:  # a listener must never break processing
                log.exception("listener failed for %s %s", event, doc_id)

    # ------------------------------------------------------------- durable job workers
    def start_workers(self) -> None:
        """Start claiming queued jobs (and jobs left running by a stopped process). Idempotent."""
        if self._stop.is_set():
            return
        with self._active_lock:
            if self._dispatcher is None or not self._dispatcher.is_alive():
                try:
                    n = self.store.release_dead_jobs()
                    if n:
                        log.info("%d interrupted job(s) queued again", n)
                except Exception:
                    log.exception("could not check for interrupted jobs")
                self._dispatcher = threading.Thread(target=self._dispatch_loop, name="docauto-jobs", daemon=True)
                self._dispatcher.start()
        self._ensure_heartbeat()

    def _ensure_heartbeat(self) -> None:
        with self._active_lock:
            if self._heart is None or not self._heart.is_alive():
                self._heart = threading.Thread(target=self._heartbeat_loop, name="docauto-heartbeat", daemon=True)
                self._heart.start()

    def close(self) -> None:
        """Stop the workers (jobs in progress finish; queued ones stay queued for the next start)."""
        self._stop.set()
        self._wake.set()
        self.pool.shutdown(wait=False)

    def _dispatch_loop(self) -> None:
        idle = JOB_POLL_MIN
        while not self._stop.is_set():
            with self._active_lock:
                free = self.max_workers - len(self._active)
            jobs = []
            if free > 0:
                try:
                    jobs = self.store.claim_jobs(self.worker, free, JOB_STALE_SECONDS)
                except Exception:
                    log.exception("could not claim jobs")
            for job in jobs:
                with self._active_lock:
                    self._active.add(job["id"])
                try:
                    self.pool.submit(self._run_claimed, job)
                except RuntimeError:     # shutting down: the job goes back to the queue
                    self.store.update_job(job["id"], "queued")
                    with self._active_lock:
                        self._active.discard(job["id"])
            if jobs:
                idle = JOB_POLL_MIN
                continue
            self._wake.wait(idle)
            if self._wake.is_set():
                self._wake.clear()
                idle = JOB_POLL_MIN
            else:
                idle = min(idle * 2, JOB_POLL_MAX)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(JOB_HEARTBEAT_SECONDS):
            with self._active_lock:
                ids = list(self._active)
            try:
                self.store.heartbeat(ids, self.worker)
            except Exception:
                log.exception("job heartbeat failed")

    def _run_claimed(self, job: dict) -> None:
        job_id, doc_id = job["id"], job["document_id"]
        try:
            if job["attempts"] > JOB_MAX_ATTEMPTS:
                self._fail_job(job_id, doc_id, {"code": "INTERRUPTED", "message":
                                                f"processing was interrupted {JOB_MAX_ATTEMPTS} times"}, time.time())
                return
            if job["attempts"] > 1:
                log.info("resuming job %s (attempt %d)", job_id, job["attempts"])
            try:
                opt = ProcessOptions.model_validate(job["request"])
            except ValueError as e:   # a request this version cannot read (e.g. from an old install)
                self._fail_job(job_id, doc_id, {"code": "INVALID_INPUT", "message": f"unreadable job request: {e}"},
                               time.time())
                return
            self._run_job(job_id, doc_id, opt)
        except Exception:
            log.exception("job %s could not run", job_id)
        finally:
            with self._active_lock:
                self._active.discard(job_id)
            self._wake.set()

    # ------------------------------------------------------------- public API
    def submit(self, data: bytes, file_name: str, options: ProcessOptions, wait: bool = False) -> dict:
        from .preprocess import sniff_mime
        import hashlib

        if not data:
            raise InvalidInput("empty file")
        doc_id = self.store.save_file(data, file_name, sniff_mime(data, file_name), hashlib.sha256(data).hexdigest())
        request = options.model_dump(mode="json", by_alias=True)
        with self.store.tx():   # the job is queued (or claimed by this thread) together with the document's state
            self.store.set_doc_meta(doc_id, options.instanceId, options.instanceVersion, options.source,
                                    doctype_id=options.doctypeId, parent_id=options.parentId)
            job_id = self.store.create_job(doc_id, request, worker=self.worker if wait else None)
        if wait:   # processed in this thread; heartbeats keep other workers off it
            with self._active_lock:
                self._active.add(job_id)
            self._ensure_heartbeat()
            try:
                self._run_job(job_id, doc_id, options)
            finally:
                with self._active_lock:
                    self._active.discard(job_id)
        else:
            self.start_workers()
            self._wake.set()
        return {"jobId": job_id, "documentId": doc_id}

    def process_bytes(self, data: bytes, file_name: str, options: ProcessOptions) -> DocumentResult:
        """Synchronous convenience used by in-process robots and tests."""
        ids = self.submit(data, file_name, options, wait=True)
        job = self.store.get_job(ids["jobId"])
        if job["status"] == "failed":
            err = job["error"] or {}
            raise DocAutoError(err.get("message", "processing failed"))
        return self.store.get_result(ids["documentId"])

    def screen_ocr(self, image: bytes, provider: str = "auto", languages: list[str] | None = None) -> DocumentResult:
        langs = languages or ["en"]
        ing = ingest(image, "screen.png", screen_mode=True)
        cands = self.router.candidates("ocr", [Capability.SCREEN_OCR], langs, provider, "screen_ocr")
        req = RecognizeRequest(document_id="screen", pages=ing.images, languages=langs,
                               capabilities=[Capability.SCREEN_OCR])
        result, attempt = self.router.run(cands, lambda p: p.instance.recognize(req))
        result.documentId = "screen"
        self._postprocess_lines(result)
        result.provenance.steps.append(ProvenanceStep(stage="digitize", providerId=attempt.provider_id,
                                                      pages=len(ing.images), fallbackFrom=attempt.fallback_from))
        return result

    def searchable_pdf(self, data: bytes, file_name: str, languages: list[str] | None = None,
                       provider: str = "auto") -> bytes:
        langs = languages or ["en"]
        ing = ingest(data, file_name)
        cands = [c for c in self.router.candidates("ocr", [Capability.SEARCHABLE_PDF], langs, provider)
                 if hasattr(c.instance, "searchable_pdf")]
        if not cands:
            raise UnsupportedCapability("no allowed engine can produce searchable PDFs")
        pdf, _ = self.router.run(cands, lambda p: p.instance.searchable_pdf(ing.images, langs))
        return pdf

    def complete_review(self, document_id: str, fields: dict[str, Any], reviewer: str,
                        reject: bool = False, classification: Optional[str] = None) -> DocumentResult:
        doc = self.store.get_result(document_id)
        if doc is None:
            raise InvalidInput(f"unknown document {document_id}")
        by_name = {f.name: f for f in doc.fields}
        meta = self.store.doc_row(document_id) or {}
        corrections = []
        for name, value in fields.items():
            f = by_name.get(name)
            if f is None:
                continue
            if not _same(f.value, value):
                corrections.append({"field": name, "original": f.value, "corrected": value})
                self.store.add_correction(meta.get("doctype_id") or meta.get("instance_id"), document_id, name,
                                          f.value, value, reviewer)
                f.originalValue, f.value, f.corrected = f.value, value, True
            f.confidence = 1.0
            f.validation.passed, f.validation.errors = True, []
        if corrections:  # point the review screen at the corrected values, where they are printed
            sid = self.store.document_schema_id(document_id)
            ground_document(doc, self.store.get_schema(sid) if sid else None, only={c["field"] for c in corrections})
        if classification and doc.classification:
            doc.classification.label, doc.classification.confidence = classification, 1.0
        doc.review.status = ReviewStatus.rejected if reject else ReviewStatus.completed
        doc.review.reviewer = reviewer
        doc.review.completedAt = datetime.now(timezone.utc)
        doc.provenance.steps.append(ProvenanceStep(stage="validate", providerId=f"human:{reviewer}"))
        status = "invalid" if reject else "validated"
        with self.store.tx():
            self.store.save_result(doc)
            self.store.set_doc_status(document_id, status, validated_by=reviewer, field_count=len(doc.fields),
                                      corrected_count=len(corrections))
            self._emit("validated", document_id, status=status, corrections=corrections, reviewer=reviewer)
        return doc

    def providers(self) -> list[dict]:
        out = []
        for d in self.registry.describe():
            p = self.registry.get(d["id"])
            ok, why = self.policy.check(p.info)
            out.append({**d, "allowedByPolicy": ok, "policyReason": why})
        return out

    # ------------------------------------------------------------- internals
    def _run_job(self, job_id: str, doc_id: str, opt: ProcessOptions) -> None:
        self.store.update_job(job_id, "running")
        self.store.set_doc_status(doc_id, "processing")
        t0 = time.time()
        try:
            result = self._process(job_id, doc_id, opt)
            status = "validation" if result.review.required else "success"
            for pp in list(self.post_processors):
                status = pp(doc_id, result, opt, status) or status
            with self.store.tx():   # status, job and what listeners queue (events, deliveries): one transaction
                self.store.update_job(job_id, {"validation": "review", "split": "completed"}.get(status, "completed")
                                      if status != "invalid" else "completed")
                self.store.set_doc_status(doc_id, status, int((time.time() - t0) * 1000),
                                          field_count=len(result.fields), corrected_count=0)
                self._emit("processed", doc_id, status=status)
        except DocAutoError as e:
            log.warning("job %s failed: %s", job_id, e.message)
            self._fail_job(job_id, doc_id, e.to_dict(), t0)
        except Exception as e:  # pragma: no cover - defensive
            log.exception("job %s crashed", job_id)
            self._fail_job(job_id, doc_id, {"code": "INTERNAL", "message": str(e)}, t0)

    def _fail_job(self, job_id: str, doc_id: str, err: dict, t0: float) -> None:
        with self.store.tx():
            self.store.update_job(job_id, "failed", err)
            self.store.set_doc_status(doc_id, "failed", int((time.time() - t0) * 1000))
            self._emit("processed", doc_id, status="failed", error=err)

    def _thresholds(self, opt: ProcessOptions) -> tuple[float, float]:
        return (opt.fieldThreshold if opt.fieldThreshold is not None else self.field_threshold,
                opt.docThreshold if opt.docThreshold is not None else self.doc_threshold)

    def _process(self, job_id: str, doc_id: str, opt: ProcessOptions) -> DocumentResult:
        data, file_name, mime = self.store.file_bytes(doc_id)
        schema = opt.schema_ or (self.store.get_schema(opt.schemaId) if opt.schemaId else None)
        if opt.schemaId and schema is None:
            raise InvalidInput(f"unknown schema '{opt.schemaId}'")

        # 0) E-INVOICE: the invoice's own XML (standalone or inside a PDF/A-3) replaces OCR and extraction
        if opt.einvoiceFirst and opt.role != "batch":
            inv = einvoice.read_file(data, mime)
            if inv:
                return self._process_einvoice(job_id, doc_id, data, file_name, mime, opt, schema, inv)

        t0 = time.time()
        ing = ingest(data, file_name, opt.pages, do_deskew=opt.deskew)
        pre_ms = int((time.time() - t0) * 1000)

        # 1) DIGITIZE (+STRUCTURE) ------------------------------------------
        all_native = (ing.mime_type == "application/pdf" and ing.images
                      and len(ing.native_text_pages) == len(ing.images))
        caps = list(dict.fromkeys(opt.capabilities or [Capability.OCR_PRINTED]))
        # barcodes are read by the core (docauto.barcodes), so the OCR engine does not need the capability
        want_barcodes = Capability.BARCODES in caps
        caps = [c for c in caps if c != Capability.BARCODES] or [Capability.OCR_PRINTED]
        if opt.useTextLayer and all_native and opt.ocrProvider in ("auto", "pdf-text"):
            cands = [self.registry.get("pdf-text")]
        else:
            cands = [c for c in self.router.candidates("ocr", caps, opt.languages, opt.ocrProvider, "document_ocr",
                                                       strict=opt.strictProvider)
                     if c.info.id != "pdf-text"]
            if not cands:
                raise UnsupportedCapability("no OCR engine left to run (pdf-text only reads digital PDFs)")
        req = RecognizeRequest(document_id=doc_id, pages=ing.images, languages=opt.languages, capabilities=caps,
                               options={"pages": opt.pages}, source_bytes=data, mime_type=ing.mime_type)

        parent = self.store.get_result(opt.reuseFrom) if opt.reuseFrom else None
        if parent is not None:
            # pages were already read when the batch was classified: reuse them (no second OCR)
            wanted = opt.reusePages or [p.pageNumber for p in parent.pages]
            pages = []
            for i, pno in enumerate(wanted, 1):
                src = next((p for p in parent.pages if p.pageNumber == pno), None)
                if src is not None:
                    cp = src.model_copy(deep=True)
                    cp.pageNumber = i
                    pages.append(cp)
            result = DocumentResult(documentId=doc_id, pages=pages)
            result.rebuild_text()
            used = next((s.providerId for s in parent.provenance.steps if s.stage == "digitize"), "reused")
            result.provenance.steps.append(ProvenanceStep(stage="digitize", providerId=used, pages=len(pages),
                                                          fallbackFrom=None))
        else:
            result, used = self._digitize(job_id, doc_id, cands, req, opt, caps)

        # weak pages: re-read by the next engines in the retry chain (local first, cloud only with opt-in)
        if parent is None and opt.ocrProvider == "auto" and used != "pdf-text":
            self._retry_weak_pages(job_id, doc_id, result, used, ing.images, opt, caps)

        result.documentId = doc_id
        result.source = Source(fileName=file_name, mimeType=ing.mime_type, sha256=ing.sha256,
                               pageCount=len(ing.images))
        for page, rot in zip(result.pages, ing.rotations):
            page.rotation = rot
        result.provenance.steps.insert(0, ProvenanceStep(stage="preprocess", providerId="core",
                                                         durationMs=pre_ms, pages=len(ing.images)))
        self._postprocess_lines(result)
        if not opt.includeRaw:
            result.raw = None

        # QR codes: asked for (barcodes), or an e-invoice QR that cross-checks the extracted fields
        qr = None
        if want_barcodes or (schema and opt.einvoiceFirst):
            n = len(ing.images) if want_barcodes else einvoice.QR_PAGES
            for page, found in zip(result.pages, barcodes.read_pages(ing.images[:n])):
                page.barcodes = found
            if schema and opt.einvoiceFirst:
                qr = einvoice.find_qr([(p.pageNumber, p.barcodes) for p in result.pages])

        # 2) CLASSIFY ---------------------------------------------------------
        if opt.classifyLabels:
            ex = self.router.candidates("extraction", [Capability.CLASSIFY], [], opt.extractionProvider, "extraction")
            cls, att = self.router.run(ex, lambda p: p.instance.classify(
                pii.PiiMasker().mask_document(result) if self._masks(p, opt) else result, opt.classifyLabels))
            result.classification = cls
            self._meter(job_id, doc_id, opt, self.registry.get(att.provider_id), Capability.CLASSIFY, 1, "classify",
                        result, att.fallback_from)

        # 3) EXTRACT -> GROUND -> QR CHECK -> (ESCALATE) -----------------------
        if schema:
            ex = self.router.candidates("extraction", [Capability.EXTRACT_SCHEMA], [], opt.extractionProvider,
                                        "extraction", strict=opt.strictProvider)
            ereq = ExtractRequest(document=result, schema=schema, images=ing.images, instructions=opt.instructions)
            t1 = time.time()
            fields, att = self.router.run(ex, lambda p: self._extract(p, ereq, opt))
            prov = self.registry.get(att.provider_id)
            model = getattr(prov.instance, "model", None)
            self._meter(job_id, doc_id, opt, prov, Capability.EXTRACT_SCHEMA, len(ing.images), "extract", result,
                        att.fallback_from, int((time.time() - t1) * 1000), model=model)
            self._refine(result, fields, schema, qr, prov)
            esc = getattr(prov.instance, "escalation_model", None)
            if esc and esc != model and self._review_reasons(result, schema, opt):
                self._escalate(job_id, doc_id, opt, prov, ereq, result, schema, qr, model, esc, len(ing.images))

        # 4) SCORE + REVIEW DECISION -----------------------------------------
        ft, dt = self._thresholds(opt)
        score(result, schema, ft, dt, opt.fieldThresholds)
        if not schema and not opt.classifyLabels:
            # plain OCR: only flag review if the caller asked for extraction
            result.review.required = False
            result.review.status = ReviewStatus.not_required
            result.warnings = [w for w in result.warnings if not w.startswith("document confidence")]
        self.store.save_result(result, schema.id if schema else None)
        return result

    def _process_einvoice(self, job_id: str, doc_id: str, data: bytes, file_name: str, mime: str,
                          opt: ProcessOptions, schema: Optional[FieldSchema], inv: dict) -> DocumentResult:
        """E-invoice XML: the fields come from the XML (confidence 1.0), no OCR. A PDF keeps its rendered pages for
        the review screen, and its text layer (free) when it has one, so the values can be highlighted."""
        t0 = time.time()
        result, n = DocumentResult(documentId=doc_id), 0
        if mime == "application/pdf":
            ing = ingest(data, file_name, opt.pages, do_deskew=opt.deskew)
            n = len(ing.images)
            if ing.images and len(ing.native_text_pages) == n:
                req = RecognizeRequest(document_id=doc_id, pages=ing.images, languages=opt.languages,
                                       capabilities=[Capability.OCR_PRINTED], options={"pages": opt.pages},
                                       source_bytes=data, mime_type=mime)
                result, _ = self._digitize(job_id, doc_id, [self.registry.get("pdf-text")], req, opt,
                                           [Capability.OCR_PRINTED])
                result.documentId = doc_id
            else:
                numbers = (opt.pages or list(range(1, n + 1)))[:n]
                result.pages = [Page(pageNumber=p, width=img.width, height=img.height)
                                for p, img in zip(numbers, ing.images)]
        result.source = Source(fileName=file_name, mimeType=mime, sha256=hashlib.sha256(data).hexdigest(),
                               pageCount=n or None)
        result.provenance.steps.insert(0, ProvenanceStep(stage="preprocess", providerId="core", pages=n,
                                                         durationMs=int((time.time() - t0) * 1000)))
        self._postprocess_lines(result)
        if opt.classifyLabels:
            label = next((l for l in opt.classifyLabels if "invoice" in l.lower()), None)
            if label:
                result.classification = Classification(label=label, confidence=1.0)
        result.fields = einvoice.xml_fields(inv, schema)
        ground_document(result, schema)       # where the values are printed (digital PDFs)
        for f in result.fields:
            if f.value not in (None, "", []) and not f.grounded:
                f.grounded, f.groundedBy = True, einvoice.XML_SOURCE
        result.einvoice = {"source": einvoice.XML_SOURCE, **inv}
        result.provenance.steps.append(ProvenanceStep(stage="extract", providerId=einvoice.XML_SOURCE, pages=n))
        ft, dt = self._thresholds(opt)
        score(result, schema, ft, dt, opt.fieldThresholds)
        self.store.save_result(result, schema.id if schema else None)
        return result

    # ------------------------------------------------------------- PII masking for cloud AI models
    def _masks(self, prov: RegisteredProvider, opt: ProcessOptions) -> bool:
        """Mask for AI engines that send the text off this machine; never for local engines."""
        if prov.info.runsLocally or prov.info.optionPack != "tier3_ai":
            return False
        return self.mask_pii_default if opt.maskPii is None else bool(opt.maskPii)

    def _extract(self, prov: RegisteredProvider, ereq: ExtractRequest, opt: ProcessOptions) -> list[ExtractedField]:
        """Extraction by one engine; for a cloud AI engine with masking on: masked text in, real values out, and no
        page images unless the process allows them (an image cannot be masked)."""
        if not self._masks(prov, opt):
            return prov.instance.extract(ereq)
        m = pii.PiiMasker()
        masked = replace(ereq, document=m.mask_document(ereq.document),
                         images=ereq.images if opt.cloudImages else [],
                         instructions=m.mask(ereq.instructions) if ereq.instructions else ereq.instructions)
        if m.masked:
            log.info("masked %d personal value(s) before sending to %s", m.masked, prov.info.id)
        return m.unmask_fields(prov.instance.extract(masked))

    def _refine(self, result: DocumentResult, fields: list[ExtractedField], schema: FieldSchema, qr: Optional[dict],
                prov: RegisteredProvider) -> None:
        """Ground the extracted fields on the pages (AI engines: hallucination guard), then cross-check them with
        the e-invoice QR code when there is one."""
        result.fields = fields
        ground_document(result, schema, ai=prov.info.optionPack == "tier3_ai")
        if qr:
            einvoice.cross_check(result, qr, schema)

    def _review_reasons(self, result: DocumentResult, schema: FieldSchema, opt: ProcessOptions) -> list[str]:
        """Why the document would go to review as extracted now (a dry run of the scorer on a copy)."""
        trial = result.model_copy(deep=True)
        trial.warnings = []
        ft, dt = self._thresholds(opt)
        score(trial, schema, ft, dt, opt.fieldThresholds)
        return trial.warnings

    def _escalate(self, job_id, doc_id, opt: ProcessOptions, prov: RegisteredProvider, ereq: ExtractRequest,
                  result: DocumentResult, schema: FieldSchema, qr: Optional[dict], model: Optional[str], esc: str,
                  pages: int) -> None:
        """Extraction below the review threshold: read again with the engine's escalation model and keep the
        better result (fewer reasons to review, then higher mean confidence). Both attempts are metered."""
        first, first_einv = [f.model_copy(deep=True) for f in result.fields], result.einvoice
        first_key = (len(self._review_reasons(result, schema, opt)), -_mean_confidence(first))
        t = time.time()
        try:
            fields, _ = self.router.run([prov], lambda p: self._extract(p, replace(ereq, model=esc), opt))
        except DocAutoError as e:
            result.warnings.append(f"escalation to {esc} failed: {e.code}")
            return
        self._meter(job_id, doc_id, opt, prov, Capability.EXTRACT_SCHEMA, pages, "extract", result, None,
                    int((time.time() - t) * 1000), model=esc)
        self._refine(result, fields, schema, qr, prov)
        better = (len(self._review_reasons(result, schema, opt)), -_mean_confidence(result.fields)) < first_key
        if not better:
            result.fields, result.einvoice = first, first_einv
        steps = [s for s in result.provenance.steps if s.stage == "extract"][-2:]
        steps[0].selected, steps[1].selected = not better, better
        result.warnings.append(f"extraction escalated from {model} to {esc}: kept the {esc if better else model} result")

    def retry_chain(self, used: str = "", languages: Optional[list[str]] = None) -> list[dict]:
        """Engines that re-read pages below the confidence threshold, in order, with why each one would or
        would not run. Local engines (retry_local) come first; the cloud engine (escalate_to) last."""
        lc = self.config.get("fallback", {}).get("low_confidence", {}) or {}
        ids = _as_list(lc.get("retry_local")) + _as_list(lc.get("escalate_to"))
        out = []
        for pid in dict.fromkeys(ids):
            p = self.registry.get(pid)
            if p is None:
                out.append({"id": pid, "provider": None, "runs": False, "reason": "unknown engine"})
                continue
            ok, why = self.policy.check(p.info)
            if pid == used:
                reason = "already the main engine"
            elif not ok:
                reason = why
            elif not p.available:
                reason = p.reason or "not available"
            elif languages and not p.info.supports_languages(languages):
                reason = "language not supported"
            else:
                reason = ""
            out.append({"id": pid, "provider": p, "runs": not reason, "reason": reason})
        return out

    def _retry_weak_pages(self, job_id, doc_id, result: DocumentResult, used: str, images, opt: ProcessOptions,
                          caps) -> None:
        threshold = float((self.config.get("fallback", {}).get("low_confidence", {}) or {}).get("threshold") or 0)
        if not threshold:
            return
        for step in self.retry_chain(used, opt.languages):
            weak = [i for i, pg in enumerate(result.pages) if (c := _page_confidence(pg)) is not None and c < threshold]
            if not weak:
                return
            if not step["runs"]:
                continue
            p, improved = step["provider"], 0
            t = time.time()
            for i in weak:
                buf = io.BytesIO()
                images[i].save(buf, format="PNG")
                req = RecognizeRequest(document_id=doc_id, pages=[images[i]], languages=opt.languages,
                                       capabilities=caps, source_bytes=buf.getvalue(), mime_type="image/png")
                try:
                    r2, _ = self.router.run([p], lambda e: e.instance.recognize(req))
                except DocAutoError as e:
                    result.warnings.append(f"retry with {p.info.id} failed: {e.code}")
                    break
                if r2.pages and (_page_confidence(r2.pages[0]) or 0) > (_page_confidence(result.pages[i]) or 0):
                    r2.pages[0].pageNumber = result.pages[i].pageNumber
                    result.pages[i] = r2.pages[0]
                    improved += 1
            log.info("retried %d weak page(s) with %s, %d improved", len(weak), p.info.id, improved)
            self._meter(job_id, doc_id, opt, p, caps[0], len(weak), "digitize", result, used,
                        int((time.time() - t) * 1000))
            if improved:
                result.rebuild_text()

    def _digitize(self, job_id, doc_id, cands, req, opt, caps, fallback_from=None):
        t = time.time()
        result, att = self.router.run(cands, lambda p: p.instance.recognize(req))
        prov = self.registry.get(att.provider_id)
        cap = Capability.TABLES if Capability.TABLES in caps else caps[0]
        self._meter(job_id, doc_id, opt, prov, cap, len(req.pages), "digitize", result,
                    att.fallback_from or fallback_from, int((time.time() - t) * 1000))
        return result, att.provider_id

    def _meter(self, job_id, doc_id, opt, prov: RegisteredProvider, cap: Capability, pages: int, stage: str,
               result: DocumentResult, fallback_from=None, duration_ms: int = 0, model: Optional[str] = None) -> None:
        credits = round(prov.info.cost([cap]) * pages, 4)
        result.provenance.steps.append(ProvenanceStep(stage=stage, providerId=prov.info.id,
                                                      providerVersion=prov.info.version, durationMs=duration_ms,
                                                      pages=pages, credits=credits, fallbackFrom=fallback_from,
                                                      model=model))
        self.store.record_usage(tenant_id=opt.tenantId, job_id=job_id, document_id=doc_id, provider_id=prov.info.id,
                                option_pack=prov.info.optionPack, capability=cap.value, pages=pages, credits=credits,
                                runs_locally=prov.info.runsLocally, regions=prov.info.regions, model=model)

    @staticmethod
    def _postprocess_lines(result: DocumentResult) -> None:
        langs = set()
        for p in result.pages:
            for l in p.lines:
                if not l.language:
                    l.language = detect_language(l.text)
                if l.direction == "ltr":
                    l.direction = detect_direction(l.text)
                if l.language:
                    langs.add(l.language)
        result.languages = sorted(langs)
        if not result.text:
            result.rebuild_text()
