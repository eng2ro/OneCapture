"""Posting date is DEFAULTED from the receipt's own date at capture.

A reviewer shouldn't have to retype the posting date when the OCR already read the
receipt date. ``ClaimService.add_line`` parses the (messy, multi-format) OCR date
into a real ``date`` and seeds the line's ``posting_date`` — overridable later, and
left blank when the date is unparseable (blank beats a wrong posting date).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from eclaim.db.models import ClaimLine
from eclaim.ocr.base import Extraction
from eclaim.services.claims import parse_receipt_date


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("02APR2026 04:50PM", dt.date(2026, 4, 2)),   # Shell — DDMMMYYYY + time
        ("23/04/26", dt.date(2026, 4, 23)),           # Lam Soon — DD/MM/YY
        ("26 SEP 2025", dt.date(2025, 9, 26)),        # Petronas — DD MMM YYYY
        ("26 Feb 2026", dt.date(2026, 2, 26)),        # Elitedrive — mixed case
        ("2026-04-02", dt.date(2026, 4, 2)),          # ISO
        ("2 April 2026", dt.date(2026, 4, 2)),        # full month
        ("2026-04-02 16:30:00", dt.date(2026, 4, 2)), # ISO + clock time
        ("16:30 no date", None),                      # time only → no date
        ("GARBAGE", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_receipt_date_formats(raw, expected):
    assert parse_receipt_date(raw) == expected


def _line_of(db_session, cid: str) -> ClaimLine:
    db_session.expire_all()
    return (
        db_session.query(ClaimLine)
        .filter(ClaimLine.claim_id == uuid.UUID(cid))
        .order_by(ClaimLine.line_no)
        .first()
    )


def test_posting_date_defaults_from_ocr_receipt_date(client, fake_ocr, db_session):
    fake_ocr.extraction = Extraction(
        expense_type="fuel_petrol", total_amount=Decimal("46.50"),
        date="02APR2026 04:50PM",
    )
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    cid = client.post("/api/claims/upload", files=files).json()["id"]

    line = _line_of(db_session, cid)
    assert line.doc_date == "02APR2026 04:50PM"       # literal receipt date preserved
    assert line.posting_date == dt.date(2026, 4, 2)   # ...and posting date seeded from it


def test_posting_date_left_blank_when_ocr_date_unparseable(client, fake_ocr, db_session):
    fake_ocr.extraction = Extraction(
        expense_type="other", total_amount=Decimal("12.00"), date="illegible",
    )
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    cid = client.post("/api/claims/upload", files=files).json()["id"]
    assert _line_of(db_session, cid).posting_date is None
