"""Phase A — audit-grade claim coding: posting gate + derived money fields."""

from __future__ import annotations

import uuid
from decimal import Decimal

from eclaim.db.models import Claim, Client
from eclaim.ocr.base import Extraction


def _upload(client, fake_ocr, extraction):
    fake_ocr.extraction = extraction
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    # Attest — an out-of-pocket claim can't release without it (P3 gate).
    return client.post("/api/claims/upload", files=files, data={"attested": "true"})


def _enable_coding_policy(db_session):
    cid = db_session.info["principal"]["client"]
    cl = db_session.get(Client, cid)
    cl.modules = {**(cl.modules or {}), "require_posting_coding": True}
    db_session.flush()


# --- posting gate ----------------------------------------------------------
def test_release_blocked_without_coding_when_policy_on(client, fake_ocr, db_session):
    _enable_coding_policy(db_session)
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("10"), unit="L",
        total_amount=Decimal("100"))).json()["id"]
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200

    # Uncoded (seed categories carry no GL; upload has no claimant cost centre).
    blocked = client.post(f"/api/claims/{cid}/release")
    assert blocked.status_code == 409
    assert "GL code and cost centre" in blocked.json()["detail"]
    assert db_session.get(Claim, uuid.UUID(cid)).status == "approved"  # not released


def test_release_succeeds_once_coded(client, fake_ocr, db_session):
    _enable_coding_policy(db_session)
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("10"), unit="L",
        total_amount=Decimal("100"))).json()["id"]
    # Code the line while still in review, then approve + release.
    client.patch(f"/api/claims/{cid}", json={
        "gl_code": "6200", "cost_centre_override": "OPS-01",
    })
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    assert client.post(f"/api/claims/{cid}/release").status_code == 200
    assert db_session.get(Claim, uuid.UUID(cid)).status == "released"


def test_release_unaffected_when_policy_off(client, fake_ocr, db_session):
    # Default client has no policy → release works without coding (back-compat).
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("10"), unit="L")).json()["id"]
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    assert client.post(f"/api/claims/{cid}/release").status_code == 200


# --- post-approval coding (pre-release Finance step) -----------------------
def test_coding_editable_after_approval_then_release(client, fake_ocr, db_session):
    """Post-approval, pre-release: Finance can still set the accounting coding on an
    APPROVED claim (so an uncoded claim can be made postable without send-back), then
    release it."""
    _enable_coding_policy(db_session)
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("10"), unit="L",
        total_amount=Decimal("100"))).json()["id"]
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    # Was blocked here before — now coding on an approved claim is allowed.
    r = client.patch(f"/api/claims/{cid}", json={
        "gl_code": "6200", "cost_centre_override": "OPS-01"})
    assert r.status_code == 200 and r.json()["gl_code"] == "6200"
    assert client.post(f"/api/claims/{cid}/release").status_code == 200
    assert db_session.get(Claim, uuid.UUID(cid)).status == "released"


def test_expense_edit_still_blocked_after_approval(client, fake_ocr, db_session):
    """The EXPENSE itself (vendor/amount/category) stays locked once approved — only
    coding may change."""
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="other", total_amount=Decimal("50"))).json()["id"]
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    blocked = client.patch(f"/api/claims/{cid}", json={"vendor": "Changed Co"})
    assert blocked.status_code == 409
    assert "only accounting coding" in blocked.json()["detail"]


# --- OCR-extracted tax ------------------------------------------------------
def test_ocr_tax_prefills_the_line(client, fake_ocr, db_session):
    """Tax read off the document (GST/SST amount + code) is stored on the line at
    capture and its net is derived — so the reviewer doesn't start from a blank tax
    field. Fails if add_line stops carrying extraction.tax_amount/tax_code."""
    from sqlalchemy import select

    from eclaim.db.models import ClaimLine

    cid = _upload(client, fake_ocr, Extraction(
        expense_type="other", total_amount=Decimal("106.00"),
        tax_amount=Decimal("6.00"), tax_code="SR",
    )).json()["id"]
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()
    assert line.tax_amount == Decimal("6.00")
    assert line.tax_code == "SR"
    assert line.net_amount == Decimal("100.00")     # gross 106 − tax 6, tax-inclusive


def test_extraction_from_item_carries_tax():
    """The pre-read capture path (item payload) carries the tax fields too."""
    from eclaim.services.ingestion import extraction_from_item

    ex = extraction_from_item(
        {"vendor": "V", "total_amount": "106", "tax_amount": "6.00", "tax_code": "SR"}
    )
    assert ex.tax_amount == Decimal("6.00") and ex.tax_code == "SR"


# --- derived money ---------------------------------------------------------
def test_edit_derives_net_from_tax_inclusive(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="other", total_amount=Decimal("106.00"))).json()["id"]
    after = client.patch(f"/api/claims/{cid}", json={"tax_amount": "6.00"}).json()
    # Tax-inclusive by default → net = gross - tax.
    assert Decimal(after["net_amount"]) == Decimal("100.00")


def test_edit_derives_base_from_fx(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="other", currency="USD", total_amount=Decimal("100.00"))).json()["id"]
    after = client.patch(f"/api/claims/{cid}", json={"fx_rate": "4.70"}).json()
    assert Decimal(after["base_amount"]) == Decimal("470.00")
