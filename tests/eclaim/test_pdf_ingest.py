"""Unit tests for PDF ingest (render / stitch) — no DB, no Anthropic.

A PDF upload becomes either one line per page (split) or one stitched line (strict
provenance); these test the pure rendering helpers the capture route relies on.
"""

from __future__ import annotations

import io

from fpdf import FPDF
from PIL import Image

from eclaim.services.documents import (
    PDF_MAX_PAGES,
    is_pdf,
    render_pdf_pages,
    stitch_pages,
)


def _pdf(pages: list[str]) -> bytes:
    doc = FPDF()
    for text in pages:
        doc.add_page()
        doc.set_font("Helvetica", size=24)
        doc.cell(0, 20, text)
    return bytes(doc.output())


def test_is_pdf_by_media_type_or_name():
    assert is_pdf("invoice.pdf", "application/octet-stream")
    assert is_pdf("x", "application/pdf")
    assert not is_pdf("photo.jpg", "image/jpeg")


def test_render_returns_one_png_per_page():
    pages = render_pdf_pages(_pdf(["Invoice A", "Invoice B", "Invoice C"]))
    assert len(pages) == 3
    for p in pages:
        im = Image.open(io.BytesIO(p))
        assert im.format == "PNG"
        assert im.width > 0 and im.height > 0


def test_render_caps_at_max_pages():
    pages = render_pdf_pages(_pdf(["p"] * (PDF_MAX_PAGES + 4)), max_pages=PDF_MAX_PAGES)
    assert len(pages) == PDF_MAX_PAGES


def test_render_rejects_non_pdf():
    try:
        render_pdf_pages(b"this is not a pdf")
    except ValueError as exc:
        assert "readable PDF" in str(exc)
    else:
        raise AssertionError("expected ValueError on a non-PDF")


def test_stitch_stacks_pages_into_one_tall_image():
    pages = render_pdf_pages(_pdf(["one", "two"]))
    per_page = [Image.open(io.BytesIO(p)) for p in pages]
    stitched = stitch_pages(pages)
    im = Image.open(io.BytesIO(stitched))
    assert im.format == "JPEG"
    # Tall image: height ≈ sum of page heights (same width → no rescale).
    assert im.width == max(p.width for p in per_page)
    assert im.height >= sum(p.height for p in per_page) - 2


def test_stitch_single_page_roundtrips():
    pages = render_pdf_pages(_pdf(["solo"]))
    im = Image.open(io.BytesIO(stitch_pages(pages)))
    assert im.width > 0 and im.height > 0
