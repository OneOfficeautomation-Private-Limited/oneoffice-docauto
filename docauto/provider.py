"""Engine option contract. Every option pack (Tier 1/2/3) implements these classes.

A provider is *pure mapping*: call the engine, map to DocumentResult. Routing,
retries, policy and metering are handled by the core service.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from PIL import Image

from .models import Classification, DocumentResult, ExtractedField, FieldSchema


class Capability(str, Enum):
    OCR_PRINTED = "ocr_printed"
    OCR_HANDWRITING = "ocr_handwriting"
    SCREEN_OCR = "screen_ocr"
    LAYOUT = "layout"
    TABLES = "tables"
    KEY_VALUE = "key_value"
    SELECTION_MARKS = "selection_marks"
    BARCODES = "barcodes"
    CLASSIFY = "classify"
    EXTRACT_SCHEMA = "extract_schema"
    PREBUILT_MODELS = "prebuilt_models"
    SEARCHABLE_PDF = "searchable_pdf"


@dataclass
class ProviderInfo:
    id: str
    displayName: str
    version: str
    vendor: str
    tier: int
    kind: str                       # "ocr" | "extraction"
    capabilities: list[Capability]
    languages: list[str]
    runsLocally: bool
    regions: list[str] = field(default_factory=list)
    creditCostPerPage: dict[str, float] = field(default_factory=dict)
    supportedMimeTypes: list[str] = field(default_factory=list)
    maxPagesPerRequest: Optional[int] = None
    credentialTypes: list[str] = field(default_factory=list)
    optionPack: str = ""            # tier1_opensource | tier2_cloud | tier3_ai

    def cost(self, capabilities: list[Capability]) -> float:
        if not capabilities:
            return min(self.creditCostPerPage.values(), default=0.0)
        return max((self.creditCostPerPage.get(c.value, 0.0) for c in capabilities), default=0.0)

    def supports_languages(self, langs: list[str]) -> bool:
        if not langs or "*" in self.languages:
            return True
        return all(l in self.languages for l in langs)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["capabilities"] = [c.value for c in self.capabilities]
        return d


@dataclass
class ProviderContext:
    config: dict[str, Any]
    credentials: dict[str, Any]
    tenant_id: str = "default"
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("docauto.provider"))


@dataclass
class RecognizeRequest:
    document_id: str
    pages: list[Image.Image]
    languages: list[str] = field(default_factory=list)
    capabilities: list[Capability] = field(default_factory=lambda: [Capability.OCR_PRINTED])
    options: dict[str, Any] = field(default_factory=dict)
    source_bytes: Optional[bytes] = None   # original file (cloud engines prefer native PDF)
    mime_type: Optional[str] = None


@dataclass
class HealthStatus:
    ok: bool
    message: str = ""


class BaseProvider:
    """Common lifecycle. Option packs subclass OcrProvider or ExtractionProvider."""

    info: ProviderInfo  # set by registry from manifest.json

    def init(self, ctx: ProviderContext) -> None:
        self.ctx = ctx

    def health_check(self) -> HealthStatus:
        return HealthStatus(True, "ok")

    def dispose(self) -> None:
        pass


class OcrProvider(BaseProvider):
    def recognize(self, req: RecognizeRequest) -> DocumentResult:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class ExtractRequest:
    document: DocumentResult
    schema: FieldSchema
    images: list[Image.Image] = field(default_factory=list)
    instructions: Optional[str] = None


class ExtractionProvider(BaseProvider):
    def extract(self, req: ExtractRequest) -> list[ExtractedField]:  # pragma: no cover - interface
        raise NotImplementedError

    def classify(self, doc: DocumentResult, labels: list[str]) -> Classification:
        raise NotImplementedError
