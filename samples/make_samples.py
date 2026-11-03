"""Generate synthetic sample documents for demos and tests (no real data).

Creates in samples/:
  invoice_scan.png      - scanned-style bilingual (EN/AR) invoice image, slightly rotated
  invoice_digital.pdf   - digital PDF with a text layer (exercises the no-OCR shortcut)
  screen.png            - small 'legacy app' screenshot for Screen OCR
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).parent
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf",
         "/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf", "arial.ttf"]
FONTS_B = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf",
           "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf"]

LINES = [
    ("ACME Trading Co. LLC", 44, True),
    ("Tax Invoice", 34, True),
    ("Invoice Number: INV-2026-0042", 30, False),
    ("Invoice Date: 15/10/2026", 30, False),
    ("Supplier VAT No: 300123456700003", 30, False),
    ("Customer: Oneoffice Automation", 30, False),
    ("Description              Qty      Amount", 28, False),
    ("RPA licence              2        8,000.00", 28, False),
    ("Support plan             1        2,000.00", 28, False),
    ("Subtotal: 10,000.00", 30, False),
    ("VAT 15%: 1,500.00", 30, False),
    ("Total: 11,500.00 SAR", 32, True),
]
ARABIC = "فاتورة ضريبية"


def font(size, bold=False):
    for f in (FONTS_B if bold else FONTS):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    raise SystemExit("No TrueType font found (DejaVu Sans or Arial). Samples were not regenerated.")


def invoice_image() -> Image.Image:
    img = Image.new("RGB", (1700, 2200), "white")
    d = ImageDraw.Draw(img)
    y = 120
    for text, size, bold in LINES:
        d.text((140, y), text, fill="black", font=font(size, bold))
        y += int(size * 2.2)
        if text == "Tax Invoice":
            try:
                d.text((1550, 120 + int(44 * 2.2)), ARABIC, fill="black", font=font(40, True),
                       direction="rtl", anchor="ra")
            except Exception:
                pass
    img = img.rotate(1.5, expand=True, fillcolor="white").filter(ImageFilter.GaussianBlur(0.6))
    return img


def digital_pdf(path: Path) -> None:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:
        print("reportlab not installed - skipping invoice_digital.pdf")
        return

    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for text, size, bold in LINES:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size * 0.45)
        c.drawString(60, y, text)
        y -= size * 0.95
    c.showPage()
    c.save()


def screen() -> Image.Image:
    img = Image.new("RGB", (640, 180), (236, 236, 236))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 640, 28], fill=(0, 51, 153))
    d.text((10, 6), "Legacy ERP - Customer Lookup", fill="white", font=font(14, True))
    d.text((20, 50), "Customer ID:  C-10293", fill="black", font=font(16))
    d.text((20, 85), "Status:       ACTIVE", fill="black", font=font(16))
    d.rectangle([20, 125, 120, 160], outline="black", fill="white")
    d.text((42, 133), "Submit", fill="black", font=font(16))
    return img


def main(out: Path = OUT, force: bool = False) -> None:
    """Write samples that are missing. The shipped samples are the reference set used by the
    tests, so existing files are kept unless force=True (fonts differ between machines)."""
    out.mkdir(parents=True, exist_ok=True)
    jobs = [("invoice_scan.png", lambda p: invoice_image().save(p)), ("invoice_digital.pdf", digital_pdf),
            ("screen.png", lambda p: screen().save(p))]
    for name, make in jobs:
        if force or not (out / name).exists():
            make(out / name)
    print("samples ready in", out)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--force"]
    main(Path(args[0]) if args else OUT, force="--force" in sys.argv)
