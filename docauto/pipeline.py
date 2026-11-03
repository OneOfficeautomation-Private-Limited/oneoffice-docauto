"""DocumentService: the orchestrator the RPA tool talks to (in-process or over REST)."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from .builtin import PDF_TEXT_INFO, PdfTextProvider
from .config import load_config
from .errors import DocAutoError, InvalidInput, UnsupportedCapability
from .extraction import RULES_INFO, RulesExtractor
from .models import DocumentResult, FieldSchema, ProvenanceStep, ReviewStatus, Source
from .normalize import detect_direction, detect_language
from .policy import PolicyEngine
from .preprocess import ingest
from .provider import Capability, ExtractRequest, RecognizeRequest
from .registry import ProviderRegistry, RegisteredProvider
from .router import Router
from .scoring import score
from .store import Store

log = logging.getLogger("docauto.pipeline")


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
    instructions: Optional[str] = None    # extra guidance for AI extractors (e.g. learned corrections)
    doctypeId: Optional[str] = None       # DocFlow document type
    parentId: Optional[str] = None        # set on documents split out of a batch
    reuseFrom: Optional[str] = None       # reuse OCR of this document instead of re-reading pages
    reusePages: Optional[list[int]] = None
    role: str = "document"                # document | batch (classify & split only)
    waitChildren: bool = False

    model_config = {"populate_by_name": True}


class DocumentService:
    def __init__(self, config: dict | None = None, config_path: str | None = None):
        self.config = config or load_config(config_path)
        self.store = Store(self.config["storage"]["data_dir"])
        self.registry = ProviderRegistry(self.config)
        self.registry.register(PdfTextProvider(), PDF_TEXT_INFO)
        self.registry.register(RulesExtractor(), RULES_INFO)
        self.registry.discover()
        self.policy = PolicyEngine(self.config)
        self.router = Router(self.registry, self.policy, self.config)
        self.pool = ThreadPoolExecutor(max_workers=int(self.config.get("workers", 4)))
        hr = self.config.get("human_review", {})
        self.field_threshold = float(hr.get("field_confidence_threshold", 0.85))
        self.doc_threshold = float(hr.get("document_confidence_threshold", 0.80))
        self.listeners: list = []        # callables(event, doc_id, **info): notified after status changes
        self.post_processors: list = []  # callables(doc_id, result, opt, status) -> new status | None (rules, split)

    def _emit(self, event: str, doc_id: str, **info) -> None:
        for fn in list(self.listeners):
            try:
                fn(event, doc_id, **info)
            except Exception:  # a listener must never break processing
                log.exception("listener failed for %s %s", event, doc_id)

    # ------------------------------------------------------------- public API
    def submit(self, data: bytes, file_name: str, options: ProcessOptions, wait: bool = False) -> dict:
        from .preprocess import sniff_mime
        import hashlib

        if not data:
            raise InvalidInput("empty file")
        doc_id = self.store.save_file(data, file_name, sniff_mime(data, file_name), hashlib.sha256(data).hexdigest())
        job_id = self.store.create_job(doc_id, options.model_dump(mode="json", by_alias=True))
        self.store.set_doc_meta(doc_id, options.instanceId, options.instanceVersion, options.source,
                                doctype_id=options.doctypeId, parent_id=options.parentId)
        if wait:
            self._run_job(job_id, doc_id, options)
        else:
            self.pool.submit(self._run_job, job_id, doc_id, options)
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
        if classification and doc.classification:
            doc.classification.label, doc.classification.confidence = classification, 1.0
        doc.review.status = ReviewStatus.rejected if reject else ReviewStatus.completed
        doc.review.reviewer = reviewer
        doc.review.completedAt = datetime.now(timezone.utc)
        doc.provenance.steps.append(ProvenanceStep(stage="validate", providerId=f"human:{reviewer}"))
        self.store.save_result(doc)
        status = "invalid" if reject else "validated"
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
            self.store.update_job(job_id, {"validation": "review", "split": "completed"}.get(status, "completed")
                                  if status != "invalid" else "completed")
            self.store.set_doc_status(doc_id, status, int((time.time() - t0) * 1000),
                                      field_count=len(result.fields), corrected_count=0)
            self._emit("processed", doc_id, status=status)
        except DocAutoError as e:
            log.warning("job %s failed: %s", job_id, e.message)
            self.store.update_job(job_id, "failed", e.to_dict())
            self.store.set_doc_status(doc_id, "failed", int((time.time() - t0) * 1000))
            self._emit("processed", doc_id, status="failed", error=e.to_dict())
        except Exception as e:  # pragma: no cover - defensive
            log.exception("job %s crashed", job_id)
            self.store.update_job(job_id, "failed", {"code": "INTERNAL", "message": str(e)})
            self.store.set_doc_status(doc_id, "failed", int((time.time() - t0) * 1000))
            self._emit("processed", doc_id, status="failed", error={"code": "INTERNAL", "message": str(e)})

    def _process(self, job_id: str, doc_id: str, opt: ProcessOptions) -> DocumentResult:
        data, file_name, _ = self.store.file_bytes(doc_id)
        t0 = time.time()
        ing = ingest(data, file_name, opt.pages, do_deskew=opt.deskew)
        pre_ms = int((time.time() - t0) * 1000)

        # 1) DIGITIZE (+STRUCTURE) ------------------------------------------
        all_native = (ing.mime_type == "application/pdf" and ing.images
                      and len(ing.native_text_pages) == len(ing.images))
        caps = list(dict.fromkeys(opt.capabilities or [Capability.OCR_PRINTED]))
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

        # low-confidence escalation (cheap engine first, premium if needed)
        esc = self.config.get("fallback", {}).get("low_confidence", {}) or {}
        conf = result.mean_line_confidence()
        if (parent is None and opt.ocrProvider == "auto" and esc.get("escalate_to") and conf is not None
                and conf < float(esc.get("threshold", 0)) and esc["escalate_to"] != used):
            target = self.registry.get(esc["escalate_to"])
            if target and target.available and self.policy.allows(target.info):
                log.info("escalating %s -> %s (conf %.2f)", used, target.info.id, conf)
                try:
                    r2, used2 = self._digitize(job_id, doc_id, [target], req, opt, caps, fallback_from=used)
                    if (r2.mean_line_confidence() or 0) >= conf:
                        result, used = r2, used2
                except DocAutoError as e:
                    result.warnings.append(f"escalation to {target.info.id} failed: {e.code}")

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

        # 2) CLASSIFY ---------------------------------------------------------
        if opt.classifyLabels:
            ex = self.router.candidates("extraction", [Capability.CLASSIFY], [], opt.extractionProvider, "extraction")
            cls, att = self.router.run(ex, lambda p: p.instance.classify(result, opt.classifyLabels))
            result.classification = cls
            self._meter(job_id, doc_id, opt, self.registry.get(att.provider_id), Capability.CLASSIFY, 1, "classify",
                        result, att.fallback_from)

        # 3) EXTRACT ----------------------------------------------------------
        schema = opt.schema_ or (self.store.get_schema(opt.schemaId) if opt.schemaId else None)
        if opt.schemaId and schema is None:
            raise InvalidInput(f"unknown schema '{opt.schemaId}'")
        if schema:
            ex = self.router.candidates("extraction", [Capability.EXTRACT_SCHEMA], [], opt.extractionProvider,
                                        "extraction", strict=opt.strictProvider)
            ereq = ExtractRequest(document=result, schema=schema, images=ing.images, instructions=opt.instructions)
            t1 = time.time()
            fields, att = self.router.run(ex, lambda p: p.instance.extract(ereq))
            result.fields = fields
            self._meter(job_id, doc_id, opt, self.registry.get(att.provider_id), Capability.EXTRACT_SCHEMA,
                        len(ing.images), "extract", result, att.fallback_from, int((time.time() - t1) * 1000))

        # 4) SCORE + REVIEW DECISION -----------------------------------------
        score(result, schema,
              opt.fieldThreshold if opt.fieldThreshold is not None else self.field_threshold,
              opt.docThreshold if opt.docThreshold is not None else self.doc_threshold)
        if not schema and not opt.classifyLabels:
            # plain OCR: only flag review if the caller asked for extraction
            result.review.required = False
            result.review.status = ReviewStatus.not_required
            result.warnings = [w for w in result.warnings if not w.startswith("document confidence")]
        self.store.save_result(result, schema.id if schema else None)
        return result

    def _digitize(self, job_id, doc_id, cands, req, opt, caps, fallback_from=None):
        t = time.time()
        result, att = self.router.run(cands, lambda p: p.instance.recognize(req))
        prov = self.registry.get(att.provider_id)
        cap = Capability.TABLES if Capability.TABLES in caps else caps[0]
        self._meter(job_id, doc_id, opt, prov, cap, len(req.pages), "digitize", result,
                    att.fallback_from or fallback_from, int((time.time() - t) * 1000))
        return result, att.provider_id

    def _meter(self, job_id, doc_id, opt, prov: RegisteredProvider, cap: Capability, pages: int, stage: str,
               result: DocumentResult, fallback_from=None, duration_ms: int = 0) -> None:
        credits = round(prov.info.cost([cap]) * pages, 4)
        result.provenance.steps.append(ProvenanceStep(stage=stage, providerId=prov.info.id,
                                                      providerVersion=prov.info.version, durationMs=duration_ms,
                                                      pages=pages, credits=credits, fallbackFrom=fallback_from))
        self.store.record_usage(tenant_id=opt.tenantId, job_id=job_id, document_id=doc_id, provider_id=prov.info.id,
                                option_pack=prov.info.optionPack, capability=cap.value, pages=pages, credits=credits,
                                runs_locally=prov.info.runsLocally, regions=prov.info.regions)

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
