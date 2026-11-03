"""Normalized data contracts (DocumentResult 1.0, FieldSchema 1.0).

Every engine option maps its output into these models. Bots, the Validation
Station and the REST API only ever see these types, never vendor payloads.

Conventions:
* Coordinates are normalized 0-1 relative to page width/height, origin top-left.
* Confidence is 0-1 or None (never invented).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


class BBox(BaseModel):
    x: float
    y: float
    w: float
    h: float


class Word(BaseModel):
    text: str
    bbox: BBox
    confidence: Optional[float] = None


class Line(BaseModel):
    text: str
    bbox: BBox
    confidence: Optional[float] = None
    language: Optional[str] = None
    direction: Literal["ltr", "rtl"] = "ltr"
    handwritten: bool = False
    wordIndexes: list[int] = Field(default_factory=list)


class Block(BaseModel):
    type: Literal["paragraph", "title", "header", "footer", "list", "figure", "table", "other"] = "paragraph"
    text: str = ""
    bbox: Optional[BBox] = None
    lineIndexes: list[int] = Field(default_factory=list)


class Cell(BaseModel):
    row: int
    col: int
    rowSpan: int = 1
    colSpan: int = 1
    text: str = ""
    isHeader: bool = False
    bbox: Optional[BBox] = None
    confidence: Optional[float] = None


class Table(BaseModel):
    rows: int
    cols: int
    cells: list[Cell] = Field(default_factory=list)
    bbox: Optional[BBox] = None
    confidence: Optional[float] = None

    def to_matrix(self) -> list[list[str]]:
        grid = [["" for _ in range(self.cols)] for _ in range(self.rows)]
        for c in self.cells:
            if c.row < self.rows and c.col < self.cols:
                grid[c.row][c.col] = c.text
        return grid


class KeyValue(BaseModel):
    key: str
    value: str = ""
    keyBbox: Optional[BBox] = None
    valueBbox: Optional[BBox] = None
    confidence: Optional[float] = None


class Page(BaseModel):
    pageNumber: int
    width: float
    height: float
    unit: Literal["pixel", "point", "inch"] = "pixel"
    rotation: float = 0.0
    blocks: list[Block] = Field(default_factory=list)
    lines: list[Line] = Field(default_factory=list)
    words: list[Word] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    keyValuePairs: list[KeyValue] = Field(default_factory=list)


class Classification(BaseModel):
    label: str
    confidence: Optional[float] = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class FieldValidation(BaseModel):
    passed: bool = True
    errors: list[str] = Field(default_factory=list)


class ExtractedField(BaseModel):
    name: str
    type: str = "string"
    value: Any = None
    rawText: Optional[str] = None
    confidence: Optional[float] = None
    page: Optional[int] = None
    bbox: Optional[BBox] = None
    providerId: str = ""
    validation: FieldValidation = Field(default_factory=FieldValidation)
    corrected: bool = False
    originalValue: Any = None


class ReviewStatus(str, Enum):
    not_required = "not_required"
    pending = "pending"
    in_review = "in_review"
    completed = "completed"
    rejected = "rejected"


class Review(BaseModel):
    required: bool = False
    status: ReviewStatus = ReviewStatus.not_required
    reviewer: Optional[str] = None
    completedAt: Optional[datetime] = None


class ProvenanceStep(BaseModel):
    stage: Literal["preprocess", "digitize", "structure", "classify", "extract", "validate"]
    providerId: str
    providerVersion: str = ""
    durationMs: int = 0
    pages: int = 0
    credits: float = 0.0
    fallbackFrom: Optional[str] = None


class Provenance(BaseModel):
    steps: list[ProvenanceStep] = Field(default_factory=list)


class Source(BaseModel):
    fileName: Optional[str] = None
    mimeType: Optional[str] = None
    sha256: Optional[str] = None
    pageCount: Optional[int] = None


class DocumentResult(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    documentId: str
    source: Source = Field(default_factory=Source)
    text: str = ""
    languages: list[str] = Field(default_factory=list)
    classification: Optional[Classification] = None
    pages: list[Page] = Field(default_factory=list)
    fields: list[ExtractedField] = Field(default_factory=list)
    confidence: Optional[float] = None
    review: Review = Field(default_factory=Review)
    provenance: Provenance = Field(default_factory=Provenance)
    warnings: list[str] = Field(default_factory=list)
    raw: Optional[dict[str, Any]] = None

    def rebuild_text(self) -> None:
        self.text = "\f".join("\n".join(l.text for l in p.lines) for p in self.pages)

    def mean_line_confidence(self) -> Optional[float]:
        vals = [l.confidence for p in self.pages for l in p.lines if l.confidence is not None]
        return sum(vals) / len(vals) if vals else None


# ---------------------------------------------------------------- FieldSchema

FieldType = Literal["string", "number", "currency", "date", "boolean", "enum", "table", "object"]


class FieldDef(BaseModel):
    name: str
    type: FieldType = "string"
    label: Optional[str] = None
    description: Optional[str] = None
    required: bool = False
    enumValues: list[str] = Field(default_factory=list)
    format: Optional[str] = None
    regex: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    columns: list["FieldDef"] = Field(default_factory=list)
    confidenceThreshold: Optional[float] = None


class FieldSchema(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    documentLanguages: list[str] = Field(default_factory=list)
    fields: list[FieldDef]
    validation: list[str] = Field(
        default_factory=list,
        description="Cross-field rules, Python-like expressions over field names, e.g. 'total >= subtotal'",
    )
