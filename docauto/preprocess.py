"""Ingest + image normalization shared by all engines."""
from __future__ import annotations

import hashlib
import io
import mimetypes
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PIL import Image, ImageOps

from .errors import InvalidInput

RENDER_DPI = 300
MAX_PAGES = 500


@dataclass
class IngestedDocument:
    file_name: str
    mime_type: str
    data: bytes
    sha256: str
    images: list[Image.Image] = field(default_factory=list)
    native_text_pages: list[int] = field(default_factory=list)   # 1-based pages with a usable text layer
    rotations: list[float] = field(default_factory=list)


def sniff_mime(data: bytes, file_name: str = "") -> str:
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    guess, _ = mimetypes.guess_type(file_name)
    return guess or "application/octet-stream"


def _pdf_pages(data: bytes, pages: Optional[list[int]]) -> tuple[list[Image.Image], list[int]]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    n = len(pdf)
    if n > MAX_PAGES:
        raise InvalidInput(f"PDF has {n} pages, limit is {MAX_PAGES}")
    wanted = [p for p in (pages or range(1, n + 1)) if 1 <= p <= n]
    images, native = [], []
    for pno in wanted:
        page = pdf[pno - 1]
        text = page.get_textpage().get_text_range() or ""
        if len(text.strip()) >= 20:
            native.append(pno)
        images.append(page.render(scale=RENDER_DPI / 72).to_pil().convert("RGB"))
    return images, native


def _raster_pages(data: bytes) -> list[Image.Image]:
    try:
        img = Image.open(io.BytesIO(data))
    except Exception as e:
        raise InvalidInput(f"unreadable image: {e}")
    frames = []
    try:
        i = 0
        while True:
            img.seek(i)
            frames.append(ImageOps.exif_transpose(img.copy()).convert("RGB"))
            i += 1
    except EOFError:
        pass
    return frames


def deskew(img: Image.Image, max_angle: float = 15.0) -> tuple[Image.Image, float]:
    """Estimate skew from dark pixels; rotate when 0.5 < |angle| <= max_angle."""
    import cv2

    gray = np.array(img.convert("L"))
    _, thr = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(thr > 0))
    if len(coords) < 500:
        return img, 0.0
    rect = cv2.minAreaRect(coords[:, ::-1].astype(np.float32))
    angle = rect[-1]
    if angle > 45:
        angle -= 90
    if abs(angle) < 0.5 or abs(angle) > max_angle:
        return img, 0.0
    return img.rotate(angle, expand=True, fillcolor="white"), float(angle)


def upscale_small(img: Image.Image, min_height: int = 1000) -> Image.Image:
    """Screenshots/snippets OCR better when enlarged."""
    if img.height >= min_height:
        return img
    factor = min(4.0, min_height / max(1, img.height))
    return img.resize((int(img.width * factor), int(img.height * factor)), Image.LANCZOS)


def ingest(data: bytes, file_name: str = "document", pages: Optional[list[int]] = None,
           do_deskew: bool = True, screen_mode: bool = False) -> IngestedDocument:
    if not data:
        raise InvalidInput("empty file")
    mime = sniff_mime(data, file_name)
    doc = IngestedDocument(file_name=file_name, mime_type=mime, data=data,
                           sha256=hashlib.sha256(data).hexdigest())
    if mime == "application/pdf":
        doc.images, doc.native_text_pages = _pdf_pages(data, pages)
    elif mime.startswith("image/"):
        frames = _raster_pages(data)
        doc.images = [f for i, f in enumerate(frames, 1) if not pages or i in pages]
    else:
        raise InvalidInput(f"unsupported file type: {mime}")
    out = []
    for img in doc.images:
        angle = 0.0
        if screen_mode:
            img = upscale_small(img)
        elif do_deskew:
            img, angle = deskew(img)
        out.append(img)
        doc.rotations.append(angle)
    doc.images = out
    return doc
