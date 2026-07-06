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


def test_stitch_checks_before_resizing_extreme_aspect(monkeypatch):
    """P4: a sliver page (tiny width, huge height) that passed its own open_guarded
    would explode when normalised UP to the widest page's width — inside .resize(),
    BEFORE the old total-height check ran. The cap must fire before any resize. The
    spy fails the test if a resize is ever allocated past the cap (i.e. the fix is
    reverted to check-after-resize)."""
    orig_resize = Image.Image.resize

    def spy_resize(self, size, *a, **k):
        w, h = size
        assert w * h <= imaging.MAX_STITCH_PIXELS, (
            f"resize allocated {w * h:,}px before the cap check")
        return orig_resize(self, size, *a, **k)

    monkeypatch.setattr(Image.Image, "resize", spy_resize)
    wide = _png(3000, 4)     # widest → sets the normalisation width
    tall = _png(4, 3000)     # normalising to 3000 wide → ~6.75e9 px height
    with pytest.raises(ImageTooLarge):
        documents.stitch_pages([wide, tall])


def test_render_clamps_extreme_aspect_page():
    """P4: an extreme-aspect page can sit under the AREA cap while one dimension
    balloons past a sane pixel size. The per-side clamp must keep both dimensions
    bounded. Without it this page renders ~40 000 px tall (fails the assertion)."""
    doc = FPDF(unit="pt", format=(20, 20000))    # a tall, thin sliver page
    doc.add_page()
    doc.set_font("Helvetica", size=8)
    doc.cell(0, 8, "sliver")
    im = Image.open(io.BytesIO(documents.render_pdf_pages(bytes(doc.output()))[0]))
    assert max(im.width, im.height) <= imaging.MAX_RENDER_SIDE
    assert im.width * im.height <= imaging.MAX_RENDER_PIXELS


# --- HEIC decode guard ------------------------------------------------------
def test_normalize_heic_does_not_swallow_bomb(monkeypatch):
    """P4: the HEIC transcode path must reject a decompression bomb, not decode it.
    open_guarded raises ImageTooLarge from the header; normalize_image must let it
    propagate (it subclasses ValueError → a clean per-receipt error), never fall
    through to convert()/save() on a gigapixel raster."""
    def boom(*a, **k):
        raise ImageTooLarge("bomb")

    monkeypatch.setattr(documents, "open_guarded", boom)
    with pytest.raises(ImageTooLarge):
        documents.normalize_image(b"fake-heic", "image/heic", name="photo.heic")


# --- segmenter bomb guard ---------------------------------------------------
def test_segmenter_thumbnail_does_not_swallow_bomb(monkeypatch):
    """P4: the segmenter thumbnailer used to swallow ImageTooLarge and return the
    ORIGINAL bytes — shipping the bomb straight to the vision API. It must propagate
    instead, and segment() must degrade to one-per-page rather than send raw pages."""
    from eclaim.ocr import segment as seg

    def boom(*a, **k):
        raise ImageTooLarge("bomb")

    monkeypatch.setattr(imaging, "open_guarded", boom)   # _thumbnail imports at call time
    with pytest.raises(ImageTooLarge):
        seg._thumbnail(b"fake-page")                     # not returned as-is

    # And the whole segmenter degrades safely (no bomb reaches the API).
    result = seg.AnthropicPageSegmenter(api_key="k").segment([b"a", b"b", b"c"])
    assert result == [[0], [1], [2]]
