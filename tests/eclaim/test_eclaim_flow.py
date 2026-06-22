"""End-to-end e-Claim tests (spec §10), against a Postgres test DB; OCR mocked.

Covers: upload+classify, release (one entry/batch + linked audit chain),
idempotent re-release, immutability + reversing correction, ledger scope totals,
and deterministic batch hash recomputed from stored rows.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from core.release import canonical_hash
from eclaim.auth.principal import Principal
from eclaim.db.models import Category, Claim, EmissionEntry
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimService, Repos
from eclaim.services.classify import DQ_SPEND, DQ_UNMAPPED
from eclaim.services.sod import SoDViolation


def _upload(client, fake_ocr, extraction: Extraction):
    fake_ocr.extraction = extraction
    files = {"file": ("receipt.png", b"\x89PNG\r\n fake-bytes", "image/png")}
    return client.post("/api/claims/upload", files=files)


def _release(client, claim_id):
    assert client.post(f"/api/claims/{claim_id}/approve").status_code == 200
    return client.post(f"/api/claims/{claim_id}/release")


# 1 -------------------------------------------------------------------------
def test_upload_classifies_activity_and_spend(client, fake_ocr):
    diesel = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L",
        total_amount=Decimal("2000"))).json()
    assert diesel["basis"] == "activity"
    assert diesel["scope"] == 1
    assert diesel["tco2e"] == "1.206000"

    elec = _upload(client, fake_ocr, Extraction(
        expense_type="electricity", quantity=Decimal("12000"), unit="kWh")).json()
    assert elec["scope"] == 2
    assert elec["tco2e"] == "7.020000"

    spend = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=None, total_amount=Decimal("500"))).json()
    assert spend["basis"] == "spend"
    assert spend["tco2e"] == "0.175000"  # 500 * 0.35 / 1000
    assert "lower data quality" in spend["data_quality"]


# 2 -------------------------------------------------------------------------
def test_release_writes_one_entry_and_linked_audit_chain(client, fake_ocr):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]
    batch = _release(client, cid).json()
    assert batch["record_count"] == 1
    assert batch["tsa_token"].startswith("STUB-TSA:")

    ledger = client.get("/api/ledger").json()
    assert len(ledger["entries"]) == 1

    events = client.get(f"/api/audit/{cid}").json()
    types = [e["event_type"] for e in events]
    assert types == ["submitted", "approved", "released", "tsa_anchored"]
    # The chain links: each prev_hash equals the previous event's hash.
    assert events[0]["prev_hash"] in (None, "")
    for prev, cur in zip(events, events[1:]):
        assert cur["prev_hash"] == prev["hash"]


# 3 -------------------------------------------------------------------------
def test_re_release_is_idempotent(client, fake_ocr):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="electricity", quantity=Decimal("12000"), unit="kWh")).json()["id"]
    first = _release(client, cid).json()
    second = client.post(f"/api/claims/{cid}/release").json()
    assert first["batch_hash"] == second["batch_hash"]
    assert len(client.get("/api/ledger").json()["entries"]) == 1


# 4 -------------------------------------------------------------------------
def test_released_claim_is_immutable_and_corrected_by_reversal(client, fake_ocr):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]
    _release(client, cid)

    # No in-place edit of a released claim.
    assert client.patch(f"/api/claims/{cid}", json={"vendor": "X"}).status_code == 409
    # No delete capability at all.
    assert client.delete(f"/api/claims/{cid}").status_code == 405

    # Correction = a reversing entry (negative tCO2e), original untouched.
    rev = client.post(f"/api/claims/{cid}/reverse").json()
    assert Decimal(rev["tco2e"]) == Decimal("-1.206000")
    entries = client.get("/api/ledger").json()["entries"]
    assert len(entries) == 2
    assert sum(Decimal(e["tco2e"]) for e in entries) == Decimal("0.000000")


# 5 -------------------------------------------------------------------------
def test_ledger_scope_totals_equal_entry_sum(client, fake_ocr):
    specs = [
        Extraction(expense_type="fuel_diesel", quantity=Decimal("450"), unit="L"),    # S1
        Extraction(expense_type="electricity", quantity=Decimal("12000"), unit="kWh"),  # S2
        Extraction(expense_type="air_travel", quantity=Decimal("1000"), unit="km"),   # S3
    ]
    for ex in specs:
        cid = _upload(client, fake_ocr, ex).json()["id"]
        _release(client, cid)

    ledger = client.get("/api/ledger").json()
    entry_sum = sum(Decimal(e["tco2e"]) for e in ledger["entries"])
    assert Decimal(ledger["scope_1"]) == Decimal("1.206000")
    assert Decimal(ledger["scope_2"]) == Decimal("7.020000")
    assert Decimal(ledger["scope_3"]) == Decimal("0.180000")
    assert Decimal(ledger["total_tco2e"]) == entry_sum


# 6 -------------------------------------------------------------------------
def test_batch_hash_recomputes_from_stored_rows(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]
    batch = _release(client, cid).json()

    claim = db_session.get(Claim, cid)
    recomputed = canonical_hash([ClaimService._projection(claim)])
    assert recomputed == batch["batch_hash"]

    entry = db_session.query(EmissionEntry).filter_by(source_id=claim.id).one()
    assert entry.carbon_ref == f"CARB-{batch['batch_hash'][:12].upper()}"


# 7 -------------------------------------------------------------------------
def test_send_back_edit_resubmit_then_approve(client, fake_ocr):
    """The send-back loop: in_review -> submitted -> (edit) -> in_review ->
    approved, with the reason and every transition captured in the audit chain."""
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]

    sb = client.post(f"/api/claims/{cid}/send-back", json={"reason": "missing GST no."})
    assert sb.status_code == 200 and sb.json()["status"] == "submitted"

    # A sent-back claim is editable (it is not released); fix it, then resubmit.
    assert client.patch(f"/api/claims/{cid}", json={"vendor": "Shell"}).status_code == 200
    rs = client.post(f"/api/claims/{cid}/resubmit")
    assert rs.status_code == 200 and rs.json()["status"] == "in_review"

    assert client.post(f"/api/claims/{cid}/approve").status_code == 200

    events = client.get(f"/api/audit/{cid}").json()
    assert [e["event_type"] for e in events] == [
        "submitted", "sent_back", "edited", "resubmitted", "approved",
    ]
    sent_back = next(e for e in events if e["event_type"] == "sent_back")
    assert sent_back["detail"]["reason"] == "missing GST no."
    # The chain still links end to end across the new events.
    for prev, cur in zip(events, events[1:]):
        assert cur["prev_hash"] == prev["hash"]


# 8 -------------------------------------------------------------------------
def test_reject_is_terminal(client, fake_ocr):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]

    rj = client.post(f"/api/claims/{cid}/reject", json={"reason": "duplicate submission"})
    assert rj.status_code == 200 and rj.json()["status"] == "rejected"

    # Terminal: no forward or backward transition out of rejected.
    assert client.post(f"/api/claims/{cid}/approve").status_code == 409
    assert client.post(f"/api/claims/{cid}/send-back", json={"reason": "x"}).status_code == 409
    assert client.post(f"/api/claims/{cid}/resubmit").status_code == 409

    events = client.get(f"/api/audit/{cid}").json()
    types = [e["event_type"] for e in events]
    assert types == ["submitted", "rejected"]
    rejected = next(e for e in events if e["event_type"] == "rejected")
    assert rejected["detail"]["reason"] == "duplicate submission"


# 9 -------------------------------------------------------------------------
def test_unauthorized_reviewer_cannot_send_back_or_reject(client, fake_ocr, db_session):
    """A viewer (no review authority) is denied at the shared SoD guard, and the
    claim stays in_review — no state change, no audit event."""
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]

    ids = db_session.info["principal"]
    viewer = Principal(
        user_id=ids["user"], firm_id=ids["firm"], base_role="viewer",
        allowed_client_ids=frozenset({ids["client"]}), email="viewer@seed.test",
    )
    repos = Repos.for_session(db_session)
    svc = ClaimService()

    with pytest.raises(SoDViolation):
        svc.send_back(repos=repos, claim_id=uuid.UUID(cid), reviewer=viewer, reason="x")
    with pytest.raises(SoDViolation):
        svc.reject(repos=repos, claim_id=uuid.UUID(cid), reviewer=viewer, reason="x")

    assert db_session.get(Claim, uuid.UUID(cid)).status == "in_review"
    # No sent_back / rejected event was written by the denied attempts.
    assert client.get(f"/api/audit/{cid}").json()[-1]["event_type"] == "submitted"


# 10 ------------------------------------------------------------------------
def test_category_drives_activity_and_spend_matched(client, fake_ocr):
    """A mapped expense_type resolves a category whose factor_key drives the EF
    lookup: activity with a usable quantity, spend-matched without — and the
    resolved category is stamped on the claim."""
    act = _upload(client, fake_ocr, Extraction(
        expense_type="electricity", quantity=Decimal("12000"), unit="kWh")).json()
    assert act["basis"] == "activity" and act["scope"] == 2
    assert act["data_quality"] == "Activity-based"
    assert act["category_id"] is not None

    sm = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=None, total_amount=Decimal("500"))).json()
    assert sm["basis"] == "spend" and sm["factor_key"] == "fuel_diesel" and sm["scope"] == 1
    assert "lower data quality" in sm["data_quality"]
    assert sm["category_id"] is not None


# 11 ------------------------------------------------------------------------
def test_null_factor_category_is_governed_spend(client, fake_ocr):
    """A category that is spend-based by intent (factor_key NULL — fuel_petrol in
    the seed) → governed spend at scope 3, marked spend-based (NOT unmapped)."""
    gov = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_petrol", total_amount=Decimal("1000"))).json()
    assert gov["basis"] == "spend" and gov["scope"] == 3
    assert gov["factor_key"] == "spend_eeio"
    assert gov["data_quality"] == DQ_SPEND
    assert gov["category_id"] is not None      # a category exists; it just has no factor


# 12 ------------------------------------------------------------------------
def test_unmapped_expense_is_flagged_not_silently_absorbed(client, fake_ocr):
    """An expense_type with no category is a valid spend_eeio row but flagged with
    a distinct, reviewable data_quality — never silently absorbed."""
    un = _upload(client, fake_ocr, Extraction(
        expense_type="other", quantity=Decimal("3"), unit="L",
        total_amount=Decimal("1000"))).json()
    assert un["scope"] == 3 and un["factor_key"] == "spend_eeio"
    assert un["data_quality"] == DQ_UNMAPPED
    assert un["data_quality"].startswith("Unmapped")
    assert un["category_id"] is None


# 13 ------------------------------------------------------------------------
def test_reviewer_assigns_category_to_clear_unmapped(client, fake_ocr, db_session):
    """A reviewer clears an unmapped claim by assigning a category; the claim
    reclassifies through that category's factor_key (qty retained → activity)."""
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="other", quantity=Decimal("450"), unit="L",
        total_amount=Decimal("2000"))).json()["id"]
    before = client.get(f"/api/claims/{cid}").json()
    assert before["data_quality"] == DQ_UNMAPPED and before["category_id"] is None

    diesel = db_session.execute(
        select(Category).filter_by(
            client_id=db_session.info["principal"]["client"], expense_type="fuel_diesel",
        )
    ).scalar_one()
    after = client.patch(f"/api/claims/{cid}", json={"category_id": str(diesel.id)}).json()

    assert after["category_id"] == str(diesel.id)
    assert after["basis"] == "activity" and after["scope"] == 1
    assert after["tco2e"] == "1.206000"        # 450 * 2.68 / 1000 — retained qty used
    assert after["data_quality"] == "Activity-based"
