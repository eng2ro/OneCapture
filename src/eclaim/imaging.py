"""Shared image-decode safety limits — decompression-bomb guard (HIGH).

A file only a few KB on disk can decode to gigapixels and exhaust memory; the
upload byte cap (B7) can't see the *decoded* size, only the wire size. So every
server-side decode goes through these limits: a process-wide PIL ceiling, a pixel
check taken from the image header (before the full decode allocates the raster),
and explicit caps for PDF-page rendering and the stitched multi-page canvas.
"""

from __future__ import annotations

import io

from PIL import Image

# One opened image (an uploaded photo / HEIC / a single page). A 24 MP phone photo
# passes; a gigapixel bomb does not.
MAX_IMAGE_PIXELS = 64_000_000
# One rendered PDF page — clamps a hostile giant MediaBox down before rasterising
# (a normal page at ~144 dpi is ~2 MP, so this is generous headroom).
MAX_RENDER_PIXELS = 8_000_000
# Cap each rendered dimension too, not only the area — an extreme-aspect page (a
# sliver MediaBox) can pass the area clamp while one side balloons to a huge strip
# that still stresses memory and downstream image ops (punch-list P4).
MAX_RENDER_SIDE = 20_000
# The whole stitched multi-page evidence image. Bounds the strict-provenance path:
# the render clamp × the page cap (30) stays under this, so a real document passes.
MAX_STITCH_PIXELS = 250_000_000

# Enforce the ceiling on ANY Image.open in the process (belt-and-suspenders for a
# decode path that doesn't call open_guarded). PIL raises DecompressionBombError
# only above 2× this; open_guarded / check_pixels below fail exactly at the cap.
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class ImageTooLarge(ValueError):
    """A decoded image would exceed the safety ceiling (likely a decompression
    bomb). Subclasses ``ValueError`` so callers that already turn a bad receipt into
    a per-item error reject it cleanly rather than crashing the whole upload."""


def check_pixels(width: int, height: int, cap: int, what: str = "image") -> None:
    """Raise :class:`ImageTooLarge` if ``width × height`` exceeds ``cap`` (or the
    dimensions are non-positive). Callers run this BEFORE allocating a raster."""
    if width <= 0 or height <= 0:
        raise ImageTooLarge(f"{what} has invalid dimensions {width}x{height}")
    if width * height > cap:
        raise ImageTooLarge(
            f"{what} too large to process safely: {width}x{height} "
            f"({width * height:,} px) exceeds the {cap:,} px cap"
        )


def open_guarded(data: bytes, *, cap: int = MAX_IMAGE_PIXELS, what: str = "image") -> Image.Image:
    """``Image.open`` plus a header-based pixel check BEFORE the full decode.
    ``Image.open`` is lazy — it reads only the header, so ``.size`` is known without
    allocating the pixel buffer — letting us reject a bomb before it costs memory.
    Returns the (not-yet-loaded) image."""
    im = Image.open(io.BytesIO(data))
    check_pixels(im.width, im.height, cap, what)
    return im
