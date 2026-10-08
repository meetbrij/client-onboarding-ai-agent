"""Render a fixture's documents as SPECIMEN images for the live KYC service.

The fixtures carry recorded extraction results (`kyc_response`). For live evals the same values are printed on a synthetic
card or bill so the real service has something to read. Every image is watermarked SPECIMEN and uses the fictional issuers
of P3's golden set. A fixture whose recorded fields are low-confidence gets a degraded (blurred, rotated) image; whether
the model then reports low confidence is measured, not forced. Output is deterministic: same fixture, same bytes.
"""

from __future__ import annotations

import io
from datetime import date

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from onboarding.fixtures import Case, CaseDocument

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
ID_LABELS = [
    ("full_name", "NAME"),
    ("date_of_birth", "DATE OF BIRTH"),
    ("id_number", "ID NUMBER"),
    ("nationality", "NATIONALITY"),
    ("expiry_date", "EXPIRY"),
    ("issuing_country", "ISSUING COUNTRY"),
]
POA_LABELS = [
    ("full_name", "ACCOUNT HOLDER"),
    ("address_line", "ADDRESS"),
    ("city", "CITY"),
    ("issue_date", "BILL DATE"),
    ("issuer", None),  # printed as the letterhead
]
DATE_FIELDS = {"date_of_birth", "expiry_date", "issue_date"}


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _fmt(name: str, value: str) -> str:
    if name in DATE_FIELDS:
        try:
            d = date.fromisoformat(value)
        except ValueError:
            return value
        return f"{d.day:02d} {MONTHS[d.month - 1]} {d.year}"
    return value


def _values(case: Case, doc_type: str) -> dict[str, str]:
    rec = case.kyc_response.get(doc_type)  # type: ignore[call-overload]
    return {f.name: f.value for f in (rec.fields if rec else []) if f.value}


def _degraded(case: Case, doc_type: str) -> bool:
    rec = case.kyc_response.get(doc_type)  # type: ignore[call-overload]
    return bool(rec and any(f.reason == "low_confidence" for f in rec.fields))


def _watermark(img: Image.Image) -> Image.Image:
    base = img.convert("RGBA")
    layer = Image.new("RGBA", (base.width * 2, base.height * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    f = _font(max(base.width // 9, 40))
    step = int(f.size * 2.6)  # type: ignore[union-attr]
    for y in range(0, layer.height, step):
        for x in range(0, layer.width, int(f.size * 6.2)):  # type: ignore[union-attr]
            draw.text((x + (y // step % 2) * f.size * 3, y), "SPECIMEN", font=f, fill=(120, 120, 120, 46))  # type: ignore[union-attr]
    layer = layer.rotate(28, resample=Image.Resampling.BICUBIC)
    left, top = (layer.width - base.width) // 2, (layer.height - base.height) // 2
    layer = layer.crop((left, top, left + base.width, top + base.height))
    return Image.alpha_composite(base, layer).convert("RGB")


def render(case: Case, doc_type: str) -> bytes:
    values = _values(case, doc_type)
    is_id = doc_type == "id_document"
    img = Image.new("RGB", (900, 560 if is_id else 640), (246, 244, 236) if is_id else (255, 255, 255))
    d = ImageDraw.Draw(img)
    if is_id:
        d.rectangle([0, 0, 900, 70], fill=(30, 70, 120))
        d.text(
            (24, 16), "REPUBLIC OF EXAMPLIA  -  NATIONAL IDENTITY CARD", font=_font(30), fill=(255, 255, 255)
        )
        d.text(
            (24, 80),
            "SPECIMEN - SYNTHETIC TEST DOCUMENT, NOT A REAL DOCUMENT",
            font=_font(18),
            fill=(150, 30, 30),
        )
        y = 130
        for name, label in ID_LABELS:
            if name in values:
                d.text((24, y), f"{label}", font=_font(18), fill=(90, 90, 90))
                d.text((24, y + 22), _fmt(name, values[name]), font=_font(30), fill=(10, 10, 10))
                y += 72
    else:
        issuer = values.get("issuer", "Imaginary Utilities Ltd")
        d.text((40, 30), issuer, font=_font(34), fill=(20, 60, 110))
        d.text((40, 80), "UTILITY BILL", font=_font(22), fill=(90, 90, 90))
        d.text(
            (40, 108),
            "SPECIMEN - SYNTHETIC TEST DOCUMENT, NOT A REAL DOCUMENT",
            font=_font(18),
            fill=(150, 30, 30),
        )
        y = 170
        for name, plabel in POA_LABELS:
            if plabel and name in values:
                d.text((40, y), plabel, font=_font(18), fill=(90, 90, 90))
                d.text((40, y + 22), _fmt(name, values[name]), font=_font(28), fill=(10, 10, 10))
                y += 72
        d.text((40, y + 20), "Amount due: 0.00 (specimen)", font=_font(20), fill=(90, 90, 90))
    img = _watermark(img)
    if _degraded(case, doc_type):
        img = img.rotate(3, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(235, 235, 235))
        img = img.filter(ImageFilter.GaussianBlur(2.2))
        img = img.resize((img.width // 2, img.height // 2)).resize((img.width, img.height))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def specimen_document(case: Case, doc: CaseDocument) -> tuple[bytes, str]:
    """DocFactory for the runner: the document as a SPECIMEN image."""
    return render(case, doc.doc_type), f"specimen_{doc.doc_type}.png"
