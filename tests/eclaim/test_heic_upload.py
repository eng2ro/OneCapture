"""iPhone HEIC/HEIF receipts are accepted and transcoded to JPEG on the way in.

The Anthropic vision API, the stored evidence image and the browser <img> viewer
none read HEIC, so ``documents.normalize_image`` transcodes it to JPEG at the
ingestion boundary. A staff member photographing a receipt with an iPhone (HEIC by
default) must be able to upload it and get a normal claim line.
"""

from __future__ import annotations

import io
import uuid
from decimal import Decimal

import pytest
from PIL import Image

from eclaim.db.models import ClaimLine
from eclaim.ocr.base import Extraction
from eclaim.services.documents import is_heic, normalize_image


def _heic_bytes() -> bytes:
    """A real HEIC image (pillow-heif encodes it)."""
    im = Image.new("RGB", (48, 64), (200, 120, 60))
    buf = io.BytesIO()
    im.save(buf, format="HEIF", quality=70)
    return buf.getvalue()


def test_is_heic_detects_by_type_and_extension():
    assert is_heic("IMG.HEIC", "image/heic")
    assert is_heic("photo.heif", "application/octet-stream")   # empty/wrong type, name saves it
    assert is_heic("x", "image/heif")
    assert not is_heic("r.jpg", "image/jpeg")


def test_normalize_transcodes_heic_to_jpeg():
    out, media = normalize_image(_heic_bytes(), "image/heic", name="IMG_1.HEIC")
    assert media == "image/jpeg"
    assert Image.open(io.BytesIO(out)).format == "JPEG"


def test_normalize_passes_non_heic_through_unchanged():
    data = b"\xff\xd8not-really-jpeg"
    assert normalize_image(data, "image/jpeg") == (data, "image/jpeg")


def test_normalize_raises_on_undecodable_heic():
    with pytest.raises(ValueError):
        normalize_image(b"not a heic at all", "image/heic", name="broken.heic")


def test_heic_upload_creates_a_line(client, fake_ocr, db_session):
    # The OCR provider is faked (tests never hit the network); the point under test is
    # that a HEIC upload is accepted + transcoded so a line is created at all.
    fake_ocr.extraction = Extraction(
        expense_type="fuel_petrol", vendor="SHELL", total_amount=Decimal("46.50"),
    )
    files = {"file": ("IMG_2043.HEIC", _heic_bytes(), "image/heic")}
    resp = client.post("/api/claims/upload", files=files)
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]

    db_session.expire_all()
    line = (
        db_session.query(ClaimLine)
        .filter(ClaimLine.claim_id == uuid.UUID(cid))
        .order_by(ClaimLine.line_no)
        .first()
    )
    assert line is not None and line.vendor == "SHELL"
    # The stored evidence image is the transcoded JPEG, not the original HEIC.
    assert line.image_path.endswith(".jpg") or line.image_path.endswith(".jpeg")
