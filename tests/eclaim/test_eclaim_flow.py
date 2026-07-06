"""End-to-end e-Claim tests (spec §10), against a Postgres test DB; OCR mocked.

e-Claim is pure claim handling — it does NO carbon maths. It keeps a per-line
``carbon_relevant`` flag and, on release, FORWARDS the raw data of relevant lines
to CarbonNext (the ``carbon_handoff`` log). These cover: upload + relevance,
release (one handoff/batch + linked audit chain), idempotent re-release,
immutability + reversing correction, the handoff log counts, and the deterministic
batch hash recomputed from stored rows.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from core.release import canonical_hash
from eclaim.auth.principal import Principal
from eclaim.db.models import CarbonHandoff, Category, Claim, ClaimLine
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimService, Repos
from eclaim.services.sod import SoDViolation


def _upload(client, fake_ocr, extraction: Extraction, *, attested: bool = True):
    fake_ocr.extraction = extraction
    files = {"file": ("receipt.png", b"\x89PNG\r\n fake-bytes", "image/png")}
    # Attest by default — an out-of-pocket claim is blocked at release without it
    # (P3 attestation gate); tests that exercise the gate pass attested=False.
    data = {"attested": "true"} if attested else None
    return client.post("/api/claims/upload", files=files, data=data)


def _release(client, claim_id):
    assert client.post(f"/api/claims/{claim_id}/approve").status_code == 200
    return client.post(f"/api/claims/{claim_id}/release")


# 1 -------------------------------------------------------------------------
def test_upload_maps_category_and_flags_relevance(client, fake_ocr):
    # No carbon maths — e-Claim resolves the category and snapshots carbon_relevant.
    diesel = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L",
        total_amount=Decimal("2000"))).json()
    assert diesel["category_id"] is not None
    assert diesel["carbon_relevant"] is True
    assert Decimal(diesel["quantity"]) == Decimal("450")  # raw activity data kept
    assert "tco2e" not in diesel and "scope" not in diesel

    # An expense_type with no category is unmapped → defaults carbon_relevant True
    # (not dropped before review).
    other = _upload(client, fake_ocr, Extraction(
        expense_type="other", total_amount=Decimal("500"))).json()
    assert other["category_id"] is None
    assert other["carbon_relevant"] is True


# 2 -------------------------------------------------------------------------
def test_release_writes_one_handoff_and_linked_audit_chain(client, fake_ocr):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]
    batch = _release(client, cid).json()
    assert batch["record_count"] == 1
    assert batch["tsa_token"].startswith("STUB-TSA:")

    ledger = client.get("/api/ledger").json()
    assert len(ledger["entries"]) == 1
    assert ledger["forwarded"] == 1

    events = client.get(f"/api/audit/{cid}").json()
    types = [e["event_type"] for e in events]
    assert types == ["submitted", "approved", "released", "tsa_anchored"]
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

    # Correction = a reversing handoff (negated amount/quantity), original untouched.
    rev = client.post(f"/api/claims/{cid}/reverse").json()
    assert rev["record_count"] == 1
    ledger = client.get("/api/ledger").json()
    entries = ledger["entries"]
    assert len(entries) == 2
    assert ledger["forwarded"] == 1 and ledger["reversed"] == 1
    # The forward + reversal cancel out.
    qsum = sum(Decimal(e["quantity"]) for e in entries if e["quantity"] is not None)
    assert qsum == Decimal("0")


# 5 -------------------------------------------------------------------------
def test_ledger_counts_forwarded_lines(client, fake_ocr):
    """The handoff log counts lines FORWARDED to CarbonNext (which owns the
    tonnage) — three carbon-relevant claims → three forwarded records."""
    specs = [
        Extraction(expense_type="fuel_diesel", quantity=Decimal("450"), unit="L"),
        Extraction(expense_type="electricity", quantity=Decimal("12000"), unit="kWh"),
        Extraction(expense_type="air_travel", quantity=Decimal("1000"), unit="km"),
    ]
    for ex in specs:
        cid = _upload(client, fake_ocr, ex).json()["id"]
        _release(client, cid)

    ledger = client.get("/api/ledger").json()
    assert ledger["forwarded"] == 3
    assert ledger["reversed"] == 0
    assert ledger["total_records"] == 3
    # No tonnage / scope on the e-Claim side — that is CarbonNext's job.
    assert all("tco2e" not in e and "scope" not in e for e in ledger["entries"])


# 6 -------------------------------------------------------------------------
def test_batch_hash_recomputes_from_stored_rows(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]
    batch = _release(client, cid).json()

    line = db_session.execute(
        select(ClaimLine).filter_by(claim_id=uuid.UUID(cid))
    ).scalar_one()
    category = db_session.get(Category, line.category_id)
    recomputed = canonical_hash([ClaimService._payload(line, category)])
    assert recomputed == batch["batch_hash"]

    handoff = db_session.query(CarbonHandoff).filter_by(line_id=line.id).one()
    assert handoff.carbon_ref == f"CARB-{batch['batch_hash'][:12].upper()}"
    assert handoff.direction == "forward"
    assert handoff.category_name == category.name  # raw data forwarded


# 7 -------------------------------------------------------------------------
def test_send_back_edit_resubmit_then_approve(client, fake_ocr):
    """The send-back loop: in_review -> submitted -> (edit) -> in_review ->
    approved, with the reason and every transition captured in the audit chain."""
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]

    sb = client.post(f"/api/claims/{cid}/send-back", json={"reason": "missing GST no."})
    assert sb.status_code == 200 and sb.json()["status"] == "submitted"

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
    for prev, cur in zip(events, events[1:]):
        assert cur["prev_hash"] == prev["hash"]


# 8 -------------------------------------------------------------------------
def test_reject_is_terminal(client, fake_ocr):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]

    rj = client.post(f"/api/claims/{cid}/reject", json={"reason": "duplicate submission"})
    assert rj.status_code == 200 and rj.json()["status"] == "rejected"

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
    claim stays in_review — and the blocked attempt is now AUDITED (governance):
    the guard records an ``approval_denied`` event instead of failing silently."""
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
    # The two blocked attempts are recorded as approval_denied (not silent).
    events = client.get(f"/api/audit/{cid}").json()
    denied = [e for e in events if e["event_type"] == "approval_denied"]
    assert len(denied) == 2
    assert {e["detail"]["action"] for e in denied} == {"send_back", "reject"}
    assert all(e["actor"] == "viewer@seed.test" for e in denied)


# 10 ------------------------------------------------------------------------
def test_mapped_category_is_stamped_and_relevant(client, fake_ocr):
    """A mapped expense_type resolves a category, which is stamped on the claim
    with its carbon_relevant flag — no scope/factor (CarbonNext's job)."""
    act = _upload(client, fake_ocr, Extraction(
        expense_type="electricity", quantity=Decimal("12000"), unit="kWh")).json()
    assert act["category_id"] is not None
    assert act["carbon_relevant"] is True

    petrol = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_petrol", total_amount=Decimal("500"))).json()
    assert petrol["category_id"] is not None


# 11 ------------------------------------------------------------------------
def test_non_relevant_category_is_not_forwarded(client, fake_ocr, db_session):
    """A category flagged carbon_relevant=False (e.g. parking) still approves and
    exports to ERP, but is NOT forwarded to CarbonNext on release."""
    ids = db_session.info["principal"]
    db_session.add(Category(
        firm_id=ids["firm"], client_id=ids["client"], name="Parking",
        expense_type="parking", carbon_relevant=False,
    ))
    db_session.flush()

    fake_ocr.extraction = Extraction(vendor="Wilson Parking", expense_type="other",
                                     total_amount=Decimal("12"))
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    cid = client.post("/api/claims/upload", files=files,
                      data={"attested": "true"}).json()["id"]
    claim = client.get(f"/api/claims/{cid}").json()
    assert claim["carbon_relevant"] is False     # merchant 'parking' → non-relevant

    _release(client, cid)
    # Nothing forwarded to CarbonNext (no relevant lines), but the claim released.
    assert client.get("/api/ledger").json()["forwarded"] == 0
    assert db_session.get(Claim, uuid.UUID(cid)).status == "released"


# 12 ------------------------------------------------------------------------
def test_unmapped_expense_has_no_category(client, fake_ocr):
    """An expense_type with no category is left unmapped (category_id None) for a
    reviewer to assign — never silently absorbed."""
    un = _upload(client, fake_ocr, Extraction(
        expense_type="other", total_amount=Decimal("1000"))).json()
    assert un["category_id"] is None
    assert un["carbon_relevant"] is True          # default: not dropped before review


# 13 ------------------------------------------------------------------------
def test_reviewer_assigns_category_to_clear_unmapped(client, fake_ocr, db_session):
    """A reviewer clears an unmapped claim by assigning a category; the line picks
    up that category and its carbon_relevant flag."""
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="other", total_amount=Decimal("2000"))).json()["id"]
    before = client.get(f"/api/claims/{cid}").json()
    assert before["category_id"] is None

    diesel = db_session.execute(
        select(Category).filter_by(
            client_id=db_session.info["principal"]["client"], expense_type="fuel_diesel",
        )
    ).scalar_one()
    after = client.patch(f"/api/claims/{cid}", json={"category_id": str(diesel.id)}).json()

    assert after["category_id"] == str(diesel.id)
    assert after["carbon_relevant"] is True
