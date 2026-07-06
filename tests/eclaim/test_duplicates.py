"""Automatic duplicate detection (Appendix A, Layer 3).

Flags (never blocks) a line that looks like an expense already recorded for this
client — the same receipt image or (vendor, amount, date) in another e-Claim, or
the same invoice number + amount already in the ERP-Sync feed.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from eclaim.db.models import Claim, ErpsyncEntry
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimService, Repos
from eclaim.services.duplicates import find_duplicates


def _claim(svc, repos, ids):
    return svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])


def _add(svc, repos, claim, fake_ocr, tmp_path, *, image=b"\x89PNG\r\n img", **fields):
    fake_ocr.extraction = Extraction(expense_type="other", **fields)
    return svc.add_line(
        repos=repos, claim=claim, image_bytes=image, media_type="image/png",
        ocr=fake_ocr, image_dir=tmp_path,
    )


def _flags_for(repos, claim):
    return find_duplicates(repos, claim, repos.claims.lines(claim.id))


def test_same_receipt_image_is_flagged(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    a = _claim(svc, repos, ids)
    _add(svc, repos, a, fake_ocr, tmp_path, image=b"SAME", vendor="Grab", total_amount=Decimal("20"))
    b = _claim(svc, repos, ids)
    _add(svc, repos, b, fake_ocr, tmp_path, image=b"SAME", vendor="Grab", total_amount=Decimal("20"))

    flags = _flags_for(repos, b)
    assert len(flags) == 1
    assert any("receipt image" in m.reason for m in flags[0]["matches"])


def test_same_vendor_amount_date_is_flagged(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    a = _claim(svc, repos, ids)
    _add(svc, repos, a, fake_ocr, tmp_path, image=b"IMG_A",
         vendor="Cafe", total_amount=Decimal("15"), date="2026-03-01")
    b = _claim(svc, repos, ids)
    _add(svc, repos, b, fake_ocr, tmp_path, image=b"IMG_B",   # different image → not an image match
         vendor="Cafe", total_amount=Decimal("15"), date="2026-03-01")

    flags = _flags_for(repos, b)
    assert len(flags) == 1
    assert any("vendor, amount & date" in m.reason for m in flags[0]["matches"])


def test_distinct_expenses_not_flagged(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    a = _claim(svc, repos, ids)
    _add(svc, repos, a, fake_ocr, tmp_path, image=b"A", vendor="Cafe",
         total_amount=Decimal("15"), date="2026-03-01")
    b = _claim(svc, repos, ids)
    _add(svc, repos, b, fake_ocr, tmp_path, image=b"B", vendor="Shop",
         total_amount=Decimal("99"), date="2026-04-04")

    assert _flags_for(repos, b) == []


def test_rejected_claim_is_not_a_duplicate(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    a = _claim(svc, repos, ids)
    _add(svc, repos, a, fake_ocr, tmp_path, image=b"SAME", vendor="Grab", total_amount=Decimal("20"))
    db_session.get(Claim, a.id).status = "rejected"      # never paid → not a dup risk
    db_session.flush()
    b = _claim(svc, repos, ids)
    _add(svc, repos, b, fake_ocr, tmp_path, image=b"SAME", vendor="Grab", total_amount=Decimal("20"))

    assert _flags_for(repos, b) == []


def test_erpsync_invoice_and_amount_is_flagged(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    db_session.add(ErpsyncEntry(
        firm_id=ids["firm"], client_id=ids["client"], doc_entry="D1", line_num=1,
        doc_number="INV-9", category="taxi", scope="scope_1", basis="spend",
        data_quality="estimated",
        amount=Decimal("50"), factor_ref="", factor_value=Decimal("0"), factor_version="v1",
        rule_id="", rule_version="v1", tco2e=Decimal("0"), source_hash="h", status="released",
    ))
    db_session.flush()

    b = _claim(svc, repos, ids)
    _add(svc, repos, b, fake_ocr, tmp_path, image=b"IMG",
         vendor="Blue Cab", doc_no="INV-9", total_amount=Decimal("50"))

    flags = _flags_for(repos, b)
    assert len(flags) == 1
    m = flags[0]["matches"][0]
    assert m.channel == "ERP-Sync" and "INV-9" in m.reference


def test_review_page_shows_duplicate_warning(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    a = _claim(svc, repos, ids)
    _add(svc, repos, a, fake_ocr, tmp_path, image=b"SAME", vendor="Grab", total_amount=Decimal("20"))
    b = _claim(svc, repos, ids)
    _add(svc, repos, b, fake_ocr, tmp_path, image=b"SAME", vendor="Grab", total_amount=Decimal("20"))

    page = client.get(f"/claims/{b.id}/review")
    assert page.status_code == 200
    assert "Possible duplicate" in page.text
