"""Phase B — receipt field highlighting: OCR bounding boxes."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from eclaim.db.models import ClaimLine
from eclaim.ocr.anthropic_provider import _coerce_boxes
from eclaim.ocr.base import Extraction


# --- tolerant box coercion (no DB) -----------------------------------------
def test_coerce_boxes_keeps_valid_drops_malformed():
    raw = {
        "vendor": [0.1, 0.1, 0.3, 0.05],     # valid
        "total_amount": [1.2, -0.2, 0.4, 0.1],  # out of range → clamped to 0..1
        "doc_no": [0.1, 0.1, 0.3],           # wrong length → dropped
        "date": "somewhere",                  # not a list → dropped
        "qty": [0.1, "x", 0.2, 0.2],          # non-numeric → dropped
    }
    out = _coerce_boxes(raw)
    assert set(out) == {"vendor", "total_amount"}
    assert out["total_amount"] == [1.0, 0.0, 0.4, 0.1]  # clamped


def test_coerce_boxes_none_when_unusable():
    assert _coerce_boxes(None) is None
    assert _coerce_boxes("nope") is None
    assert _coerce_boxes({"x": [1, 2, 3]}) is None  # all dropped → None


def test_extraction_carries_boxes():
    ex = Extraction(vendor="Shell", boxes={"vendor": [0.1, 0.1, 0.2, 0.05]})
    assert ex.boxes["vendor"] == [0.1, 0.1, 0.2, 0.05]
    assert "boxes" in ex.model_dump()


# --- persistence + render (DB) ---------------------------------------------
def _upload(client, fake_ocr, extraction):
    fake_ocr.extraction = extraction
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    return client.post("/api/claims/upload", files=files)


def test_upload_persists_ocr_boxes(client, fake_ocr, db_session):
    boxes = {"vendor": [0.05, 0.04, 0.5, 0.06], "total_amount": [0.6, 0.8, 0.3, 0.07]}
    cid = _upload(client, fake_ocr, Extraction(
        vendor="Shell", expense_type="fuel_diesel", total_amount=Decimal("70"),
        boxes=boxes)).json()["id"]
    line = db_session.execute(
        select(ClaimLine).filter_by(claim_id=uuid.UUID(cid))
    ).scalar_one()
    assert line.ocr_boxes == boxes


def test_web_capture_persists_boxes(client, db_session):
    """The web capture path (receipt read client-side) must carry the field boxes
    through the items payload to the saved line — not just the API upload path."""
    import json
    from eclaim.db.models import Claim
    boxes = {"vendor": [0.05, 0.04, 0.5, 0.06], "total_amount": [0.6, 0.8, 0.3, 0.07]}
    items = [{
        "expense_type": "fuel_diesel", "quantity": "100", "unit": "L",
        "total_amount": "500", "vendor": "Shell", "boxes": boxes,
    }]
    resp = client.post(
        "/capture",
        files=[("files", ("r.png", b"\x89PNG\r\n fake", "image/png"))],
        data={"items": json.dumps(items), "attested": "yes"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    claim = db_session.execute(select(Claim)).scalars().one()
    line = db_session.execute(
        select(ClaimLine).filter_by(claim_id=claim.id)
    ).scalar_one()
    assert line.ocr_boxes == boxes


def test_review_page_renders_box_overlay(client, fake_ocr):
    cid = _upload(client, fake_ocr, Extraction(
        vendor="Shell", expense_type="fuel_diesel", total_amount=Decimal("70"),
        boxes={"vendor": [0.05, 0.04, 0.5, 0.06]})).json()["id"]
    page = client.get(f"/claims/{cid}/review").text
    assert "box-chips" in page and "renderBoxes" in page
    assert "LINE_BOXES" in page
    assert "0.05" in page          # the box coordinate is embedded for the overlay
