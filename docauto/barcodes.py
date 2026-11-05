"""The core's barcode reader (capability "barcodes"): QR codes on page images with OpenCV.

cv2 is already installed for deskewing, so no OCR engine has to offer barcodes: the pipeline takes the
capability off the engine requirements and reads the codes itself (Page.barcodes). E-invoice QR codes
(ZATCA, India GST) found this way are cross-checked against the extracted fields (docauto.einvoice).
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from PIL import Image

from .models import BBox, Barcode
from .normalize import clamp01

log = logging.getLogger("docauto.barcodes")
MAX_SIDE = 2600   # larger pages are scaled down first (a 300 dpi A4 is 2480 x 3508); codes stay readable


def _bbox(points, w: int, h: int, scale: float) -> Optional[BBox]:
    try:
        pts = np.asarray(points, dtype=float).reshape(-1, 2) / scale
    except (TypeError, ValueError):
        return None
    x0, y0 = pts.min(axis=0)
    x1, y1 = pts.max(axis=0)
    return BBox(x=clamp01(x0 / w), y=clamp01(y0 / h), w=clamp01((x1 - x0) / w), h=clamp01((y1 - y0) / h))


def read_qr(img: Image.Image) -> list[Barcode]:
    """Every QR code OpenCV can decode on one page image (box normalized 0-1 like OCR words)."""
    import cv2

    gray = np.array(img.convert("L"))
    h, w = gray.shape
    scale = min(1.0, MAX_SIDE / max(h, w))
    if scale < 1.0:
        gray = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    det = cv2.QRCodeDetector()
    out: list[Barcode] = []
    try:
        ok, texts, points, _ = det.detectAndDecodeMulti(gray)
        if ok:
            for text, pts in zip(texts, points if points is not None else []):
                if text:
                    out.append(Barcode(type="qr", text=text, bbox=_bbox(pts, w, h, scale)))
        if not out:  # the single-code detector finds some codes the multi one misses
            text, pts, _ = det.detectAndDecode(gray)
            if text:
                out.append(Barcode(type="qr", text=text, bbox=_bbox(pts, w, h, scale)))
    except cv2.error as e:  # an unreadable page must not stop processing
        log.warning("QR reader failed: %s", e)
    return out


def read_pages(images: list[Image.Image]) -> list[list[Barcode]]:
    return [read_qr(img) for img in images]
