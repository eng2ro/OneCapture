"""Turn an uploaded PDF into receipt images for the capture pipeline.

A PDF may hold one multi-page invoice or several invoices. We render its pages to
images with pypdfium2 (self-contained wheel — no system Poppler to install) and
let the caller decide, per the client's ``allow_document_split`` policy, whether:

* each page becomes its own claim line (split — one page ≈ one invoice), or
* all pages are stitched into one tall image kept as a single line's multi-page
  evidence (strict 1:1 provenance — the source document is never re-segmented).

Rendering to images means the OCR provider, the receipt viewer and the evidence
pack all stay unchanged — a PDF-derived line looks like any image receipt.
"""

from __future__ import annotations

import io

import pillow_heif
import pypdfium2 as pdfium
from PIL import Image, ImageOps

# Teach PIL to open HEIF/HEIC (iPhone photos) so ``normalize_image`` can transcode
# them. Registering the opener is idempotent and cheap.
pillow_heif.register_heif_opener()

# Bound the work one upload can trigger (a hostile / huge PDF).
PDF_MAX_PAGES = 30
_RENDER_SCALE = 2.0     # ~144 dpi at Letter — enough for OCR without being huge

# iPhone/Android HEIF photos. Neither the Anthropic vision API, the stored evidence
# image, nor the browser <img> viewer read HEIC — so we transcode to JPEG on the way in.
_HEIC_MEDIA = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}


def is_pdf(name: str, media_type: str) -> bool:
    return media_type == "application/pdf" or (name or "").lower().endswith(".pdf")


def is_heic(name: str, media_type: str) -> bool:
    return media_type in _HEIC_MEDIA or (name or "").lower().endswith((".heic", ".heif"))


def normalize_image(data: bytes, media_type: str, *, name: str = "") -> tuple[bytes, str]:
    """Return ``(bytes, media_type)`` the rest of the pipeline can handle: an iPhone
    HEIC/HEIF photo is transcoded to JPEG (with EXIF orientation baked in so the
    receipt isn't sideways); anything else passes through untouched. Raises
    ``ValueError`` if the HEIC can't be decoded, so the caller records a per-receipt
    error and skips it rather than crashing the whole upload."""
    if not is_heic(name, media_type):
        return data, media_type
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except Exception as exc:
        raise ValueError("could not read HEIC/HEIF image") from exc
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"


def render_pdf_pages(data: bytes, *, max_pages: int = PDF_MAX_PAGES) -> list[bytes]:
    """Render up to ``max_pages`` pages to PNG bytes (one per page). Raises
    ``ValueError`` on a PDF pypdfium2 can't open."""
    try:
        doc = pdfium.PdfDocument(data)
    except Exception as exc:   # pdfium raises its own exception types
        raise ValueError("not a readable PDF") from exc
    try:
        pages: list[bytes] = []
        for i in range(min(len(doc), max_pages)):
            pil = doc[i].render(scale=_RENDER_SCALE).to_pil().convert("RGB")
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            pages.append(buf.getvalue())
        return pages
    finally:
        doc.close()


def stitch_pages(pngs: list[bytes]) -> bytes:
    """Stack page images top-to-bottom into one tall JPEG (normalised to the widest
    page). This IS the whole document as a single viewable evidence image — what a
    strict-provenance client gets instead of split-per-page lines."""
    imgs = [Image.open(io.BytesIO(p)).convert("RGB") for p in pngs]
    width = max(im.width for im in imgs)
    scaled = []
    for im in imgs:
        if im.width != width:
            im = im.resize((width, round(im.height * width / im.width)))
        scaled.append(im)
    canvas = Image.new("RGB", (width, sum(im.height for im in scaled)), "white")
    y = 0
    for im in scaled:
        canvas.paste(im, (0, y))
        y += im.height
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
