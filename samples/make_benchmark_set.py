"""Generate a synthetic, labelled invoice test set for the accuracy benchmark (no real data).

Every invoice gets random supplier, numbers, dates and line items, and is written in several
variants that mimic how documents arrive (the clean-up presets in the OCR strategy plan):

  clean   rendered page, no noise                       .png
  scan    greyscale, slight rotation, blur, noise         .jpg
  fax     ~100 dpi, black and white, speckles           .png
  photo   phone photo: perspective, uneven light, JPEG  .jpg
  pdf     digital PDF with a text layer (no OCR needed)  .pdf

Each document has a sibling <name>.truth.json with the correct field values, the full text
and its variant as a tag. Fields follow samples/invoice_schema.json.

    python samples/make_benchmark_set.py [out_dir] [--count 10] [--seed 7] [--variants clean,scan]

The default out_dir is benchmark-data/synthetic (git-ignored).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
VARIANTS = ("clean", "scan", "fax", "photo", "pdf")
SUFFIX = {"clean": ".png", "scan": ".jpg", "fax": ".png", "photo": ".jpg", "pdf": ".pdf"}
JPEG_QUALITY = {"scan": 80, "photo": 60}

SUPPLIERS = ["ACME Trading Co. LLC", "Gulf Office Supplies", "Nile Logistics Ltd", "Red Sea Print House",
             "Delta Industrial Parts", "Falcon IT Services", "Oasis Catering Co.", "Summit Facilities LLC"]
CUSTOMERS = ["Oneoffice Automation", "Al Noor Holdings", "Crescent Medical Centre", "Blue Harbor Retail"]
ITEMS = [("RPA licence", 4000), ("Support plan", 2000), ("Printer toner", 350), ("A4 paper (box)", 95),
         ("Laptop stand", 180), ("Network switch", 1250), ("Cloud backup (1 yr)", 900),
         ("On-site training", 1500), ("Office chairs", 640), ("Delivery charges", 75)]
VAT_RATE = 0.15


def _money(v: float) -> str:
    return f"{v:,.2f}"


def make_invoice(rng: random.Random, index: int) -> dict:
    """Random invoice data plus the lines printed on the page (in reading order)."""
    items = []
    for name, price in rng.sample(ITEMS, rng.randint(2, 5)):
        qty = rng.randint(1, 12)
        unit = round(price * rng.uniform(0.7, 1.3), 2)
        items.append((name, qty, round(qty * unit, 2)))
    subtotal = round(sum(a for _, _, a in items), 2)
    vat = round(subtotal * VAT_RATE, 2)
    total = round(subtotal + vat, 2)
    inv_date = date(2026, 1, 1) + timedelta(days=rng.randint(0, 300))
    number = f"INV-{inv_date.year}-{rng.randint(1, 9999):04d}"
    vat_no = "3" + "".join(str(rng.randint(0, 9)) for _ in range(13)) + "3"
    supplier = rng.choice(SUPPLIERS)
    fields = {"invoice_number": number, "invoice_date": inv_date.isoformat(), "supplier_vat": vat_no,
              "subtotal": subtotal, "vat_amount": vat, "total": total}
    lines = [
        (supplier, 44, True),
        ("Tax Invoice", 34, True),
        (f"Invoice Number: {number}", 30, False),
        (f"Invoice Date: {inv_date.strftime('%d/%m/%Y')}", 30, False),
        (f"{rng.choice(['Supplier VAT No', 'VAT Number'])}: {vat_no}", 30, False),
        (f"Customer: {rng.choice(CUSTOMERS)}", 30, False),
        ("Description              Qty      Amount", 28, False),
    ]
    for name, qty, amount in items:
        lines.append((f"{name:<25}{qty:<9}{_money(amount)}", 28, False))
    lines += [
        (f"Subtotal: {_money(subtotal)}", 30, False),
        (f"VAT 15%: {_money(vat)}", 30, False),
        (f"Total: {_money(total)} SAR", 32, True),
    ]
    return {"name": f"inv{index:03d}", "fields": fields, "lines": lines}


# ------------------------------------------------------------------ rendering
def render(inv: dict) -> Image.Image:
    from make_samples import font  # same font lookup as the shipped samples

    img = Image.new("RGB", (1700, 2200), "white")
    d = ImageDraw.Draw(img)
    y = 120
    for text, size, bold in inv["lines"]:
        d.text((140, y), text, fill="black", font=font(size, bold))
        y += int(size * 2.2)
    return img


def scan(img: Image.Image, nrng: np.random.Generator) -> Image.Image:
    img = img.convert("L").rotate(float(nrng.uniform(-2, 2)), expand=True, fillcolor=255, resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(float(nrng.uniform(0.5, 0.9))))
    a = np.asarray(img).astype(np.float32) * 0.94 + 6           # light grey paper
    a += nrng.normal(0, 7, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).convert("RGB")


def fax(img: Image.Image, nrng: np.random.Generator) -> Image.Image:
    small = img.convert("L").resize((img.width // 2, img.height // 2), Image.BILINEAR)
    small = small.rotate(float(nrng.uniform(-1, 1)), expand=True, fillcolor=255)
    a = np.asarray(small)
    bw = np.where(a < 170, 0, 255).astype(np.uint8)
    specks = nrng.random(bw.shape)
    bw[specks < 0.002] = 0
    bw[specks > 0.999] = 255
    return Image.fromarray(bw).convert("RGB")


def photo(img: Image.Image, nrng: np.random.Generator) -> Image.Image:
    import cv2

    w, h = img.size
    pad = 120
    canvas = Image.new("RGB", (w + 2 * pad, h + 2 * pad), (92, 84, 76))   # desk behind the page
    canvas.paste(img, (pad, pad))
    a = np.asarray(canvas)
    src = np.float32([[pad, pad], [pad + w, pad], [pad + w, pad + h], [pad, pad + h]])
    jit = nrng.uniform(-0.03, 0.03, (4, 2)) * [w, h]
    dst = np.float32(src + jit)
    m = cv2.getPerspectiveTransform(src, dst)
    a = cv2.warpPerspective(a, m, (canvas.width, canvas.height), borderValue=(92, 84, 76)).astype(np.float32)
    gx = np.linspace(float(nrng.uniform(0.72, 0.9)), 1.0, a.shape[1])[None, :, None]   # uneven light
    gy = np.linspace(1.0, float(nrng.uniform(0.8, 0.95)), a.shape[0])[:, None, None]
    a = a * gx * gy * [1.0, 0.97, 0.9]                                                  # warm tint
    out = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.0))
    return out.resize((int(out.width * 0.75), int(out.height * 0.75)), Image.LANCZOS)


def write_pdf(inv: dict, path: Path) -> bool:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:
        return False
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for text, size, bold in inv["lines"]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size * 0.45)
        c.drawString(60, y, text)
        y -= size * 0.95
    c.showPage()
    c.save()
    return True


# ------------------------------------------------------------------ set
def make_set(out: Path, count: int = 10, seed: int = 7, variants: tuple[str, ...] = VARIANTS) -> list[Path]:
    """Write the test set; returns the document paths. The same seed gives the same invoices."""
    unknown = set(variants) - set(VARIANTS)
    if unknown:
        raise ValueError(f"unknown variants {sorted(unknown)}, expected some of {VARIANTS}")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)
    written = []
    for i in range(1, count + 1):
        inv = make_invoice(rng, i)
        text = "\n".join(t for t, _, _ in inv["lines"])
        page = None
        for v in variants:
            path = out / f"{inv['name']}_{v}{SUFFIX[v]}"
            if v == "pdf":
                if not write_pdf(inv, path):
                    print("reportlab not installed - skipping pdf variants")
                    continue
            else:
                if page is None:
                    page = render(inv)
                img = {"clean": lambda p: p, "scan": lambda p: scan(p, nrng), "fax": lambda p: fax(p, nrng),
                       "photo": lambda p: photo(p, nrng)}[v](page)
                if v in JPEG_QUALITY:
                    img.save(path, quality=JPEG_QUALITY[v])
                else:
                    img.save(path)
            truth = {"fields": inv["fields"], "text": text, "tags": [v]}
            path.with_suffix(".truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")
            written.append(path)
    # no "schema" key: the benchmark then uses samples/invoice_schema.json
    (out / "set.json").write_text(json.dumps({"name": f"synthetic invoices (seed {seed})", "languages": ["en"]},
                                             indent=2), encoding="utf-8")
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", nargs="?", default=str(ROOT / "benchmark-data" / "synthetic"))
    ap.add_argument("--count", type=int, default=10, help="number of invoices (each in every variant)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--variants", default=",".join(VARIANTS))
    a = ap.parse_args(argv)
    paths = make_set(Path(a.out), a.count, a.seed, tuple(v.strip() for v in a.variants.split(",") if v.strip()))
    print(f"{len(paths)} documents written to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
