"""SDK helpers used by option packs to map vendor output into DocumentResult."""
from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

from .models import BBox

_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def bbox_from_pixels(left: float, top: float, width: float, height: float,
                     page_w: float, page_h: float) -> BBox:
    if page_w <= 0 or page_h <= 0:
        raise ValueError("page size must be positive")
    return BBox(x=clamp01(left / page_w), y=clamp01(top / page_h),
                w=clamp01(width / page_w), h=clamp01(height / page_h))


def polygon_to_bbox(points: Sequence[Sequence[float]], page_w: float = 1.0, page_h: float = 1.0) -> BBox:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return bbox_from_pixels(x0, y0, x1 - x0, y1 - y0, page_w, page_h)


def flat_polygon_to_points(flat: Sequence[float]) -> list[tuple[float, float]]:
    """Azure-style [x1,y1,x2,y2,...] -> [(x1,y1),...]."""
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]


def conf100(v: Optional[float]) -> Optional[float]:
    if v is None or v < 0:
        return None
    return clamp01(v / 100.0)


def union_bbox(boxes: Iterable[BBox]) -> Optional[BBox]:
    boxes = list(boxes)
    if not boxes:
        return None
    x0 = min(b.x for b in boxes)
    y0 = min(b.y for b in boxes)
    x1 = max(b.x + b.w for b in boxes)
    y1 = max(b.y + b.h for b in boxes)
    return BBox(x=x0, y=y0, w=x1 - x0, h=y1 - y0)


def detect_direction(text: str) -> str:
    """'rtl' when most letters are Arabic script."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "ltr"
    ar = sum(1 for c in letters if _ARABIC.match(c))
    return "rtl" if ar / len(letters) > 0.5 else "ltr"


def detect_language(text: str) -> Optional[str]:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return None
    ar = sum(1 for c in letters if _ARABIC.match(c))
    return "ar" if ar / len(letters) > 0.5 else "en"
