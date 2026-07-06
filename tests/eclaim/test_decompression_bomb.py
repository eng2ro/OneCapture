"""Decompression-bomb guards (HIGH): a small file can decode to gigapixels and
exhaust memory. Every server-side decode is bounded — the header-based open guard,
the PDF-page render clamp, and the stitched-canvas cap — plus PIL's global ceiling.
"""

from __future__ import annotations

import io

import pytest
from fpdf import FPDF
from PIL import Image

from eclaim import imaging
from eclaim.imaging import ImageTooLarge, check_pixels, open_guarded
from eclaim.services import documents


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


# --- primitives -------------------------------------------------------------
def test_check_pixels_enforces_cap():
    check_pixels(100, 100, 20_000)                       # under cap → ok
    with pytest.raises(ImageTooLarge):
        check_pixels(100, 100, 5_000)                    # 10 000 px > 5 000 cap
    with pytest.raises(ImageTooLarge):
        check_pixels(0, 100, 5_000)                      # non-positive dim


def test_open_guarded_rejects_from_header_without_full_decode():
    data = _png(200, 200)                                # 40 000 px, small on disk
    with pytest.raises(ImageTooLarge):
        open_guarded(data, cap=10_000)                   # rejected by header dims
    im = open_guarded(data, cap=1_000_000)               # under cap → opens fine
    assert im.size == (200, 200)


def test_global_pil_ceiling_is_armed():
    assert Image.MAX_IMAGE_PIXELS == imaging.MAX_IMAGE_PIXELS


# --- PDF render clamp -------------------------------------------------------
def test_render_clamps_a_giant_page():
    # A single enormous page: at the default scale this would rasterise to hundreds
    # of megapixels; the clamp must keep it within MAX_RENDER_PIXELS.
    doc = FPDF(unit="pt", format=(8000, 8000))
    doc.add_page()
    doc.set_font("Helvetica", size=48)
    doc.cell(0, 40, "huge")
    pages = documents.render_pdf_pages(bytes(doc.output()))
    assert len(pages) == 1
    im = Image.open(io.BytesIO(pages[0]))
    assert im.width * im.height <= imaging.MAX_RENDER_PIXELS


def test_render_normal_page_is_unclamped():
    doc = FPDF()
    doc.add_page()
    doc.set_font("Helvetica", size=24)
    doc.cell(0, 20, "normal invoice")
    im = Image.open(io.BytesIO(documents.render_pdf_pages(bytes(doc.output()))[0]))
    assert im.width * im.height <= imaging.MAX_RENDER_PIXELS
    assert im.width > 200 and im.height > 200        # a real, legible render


# --- stitch canvas cap ------------------------------------------------------
def test_stitch_rejects_oversize_canvas(monkeypatch):
    monkeypatch.setattr(documents, "MAX_STITCH_PIXELS", 1_000)
    with pytest.raises(ImageTooLarge):
        documents.stitch_pages([_png(50, 50), _png(50, 50)])   # 50 × 100 = 5 000 px


def test_stitch_under_cap_still_works():
    # The real cap comfortably fits a normal stitch (happy path stays intact).
    out = documents.stitch_pages([_png(50, 50), _png(50, 50)])
    assert Image.open(io.BytesIO(out)).size == (50, 100)


def test_stitch_rejects_empty():
    with pytest.raises(ValueError):
        documents.stitch_pages([])
