"""Pre-UAT audit fixes (2026-07-10 head-to-toe audit): the 2 blockers + the
security/control majors, each pinned so they cannot silently regress.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import Request
from sqlalchemy import select

from eclaim.db.models import AuditEvent, Claim, ClaimLine
from eclaim.services.claims import ClaimError, ClaimService


# --- Blocker 2: header totals are HOME currency (base_amount), not a cross- ---
# --- currency gross sum that would show wrong RM and mis-gate approvals -------
def test_recompute_totals_uses_home_base_not_cross_currency_gross():
    lines = [
        SimpleNamespace(base_amount=Decimal("50.00"), line_status="approved",
                        payment_method="out_of_pocket"),      # RM 50
        SimpleNamespace(base_amount=Decimal("900.00"), line_status="approved",
                        payment_method="out_of_pocket"),      # SGD 300 → RM 900
    ]
    claim = SimpleNamespace(total_claimed=None, total_approved=None,
                            total_reimbursable=None)
    ClaimService._recompute_totals(claim, lines)
    assert claim.total_claimed == Decimal("950.00")           # NOT 350 (gross sum)
    assert claim.total_approved == Decimal("950.00")
    assert claim.total_reimbursable == Decimal("950.00")


def test_require_fx_resolved_blocks_unconverted_foreign_line():
    lines = [SimpleNamespace(total_amount=Decimal("100"), base_amount=None)]
    with pytest.raises(ClaimError, match="exchange rate"):
        ClaimService._require_fx_resolved(lines)
    # A fully-converted (or MYR) set passes.
    ClaimService._require_fx_resolved(
        [SimpleNamespace(total_amount=Decimal("100"), base_amount=Decimal("100"))]
    )


# --- Blocker 1: a queried claim (decide → sent_back) can be resubmitted -------
def _upload(client, fake_ocr):
    from eclaim.ocr.base import Extraction

    fake_ocr.extraction = Extraction(expense_type="other", total_amount=Decimal("40"))
    files = {"file": ("r.png", b"\x89PNG uat", "image/png")}
    return client.post("/api/claims/upload", files=files,
                       data={"attested": "true"}).json()["id"]


def test_queried_claim_is_not_stranded_and_resubmits(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr)
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()

    # Reviewer queries the line → header rolls up to 'sent_back'.
    r = client.post(f"/claims/{cid}/decide", data={
        f"line_{line.id}": "queried", f"reason_{line.id}": "need itemised receipt",
    }, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Claim, uuid.UUID(cid)).status == "sent_back"

    # The recovery button the UI shows (Resubmit) now works instead of erroring.
    r = client.post(f"/claims/{cid}/resubmit", follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Claim, uuid.UUID(cid)).status == "in_review"


# --- Major: matrix changes are written to the tamper-evident audit chain ------
def test_matrix_changes_are_audited(client, db_session):
    ids = db_session.info["principal"]
    r = client.post("/admin/approvals/add", data={
        "client_id": str(ids["client"]), "min_amount": "0", "max_amount": "1000",
        "approver_role": "manager", "scope_module": "eclaim",
    }, follow_redirects=False)
    assert r.status_code == 303
    events = db_session.execute(
        select(AuditEvent).where(AuditEvent.entity_type == "approval_matrix")
    ).scalars().all()
    assert any(e.event_type == "matrix_rule_added" for e in events)


# --- Major: viewer cannot advance a claim via the JSON API resubmit -----------
def test_api_resubmit_blocks_viewer(client, fake_ocr, db_session):
    """The bearer resubmit endpoint must pass the principal so the writer/viewer
    gate fires (every other claim-mutating endpoint does; resubmit alone skipped
    it). Swap the working fixture app's principal to a viewer and hit the route."""
    from eclaim.api import deps
    from eclaim.auth.principal import Principal

    cid = _upload(client, fake_ocr)              # created by the partner client
    db_session.get(Claim, uuid.UUID(cid)).status = "sent_back"
    db_session.commit()

    ids = db_session.info["principal"]

    def _viewer(request: Request) -> Principal:
        p = Principal(
            user_id=ids["user"], firm_id=ids["firm"], base_role="viewer",
            allowed_client_ids=frozenset({ids["client"]}), email="v@seed.test",
        )
        request.state.principal = p
        request.state.db = db_session
        return p

    # The fixture tears the app down after the test, so no restore needed.
    client.app.dependency_overrides[deps.get_principal] = _viewer
    client.app.dependency_overrides[deps.get_session_principal] = _viewer
    r = client.post(f"/api/claims/{cid}/resubmit")
    assert r.status_code == 403
    db_session.expire_all()
    assert db_session.get(Claim, uuid.UUID(cid)).status == "sent_back"   # unchanged


# --- Major: claims CSV export neutralises spreadsheet formula injection --------
def test_claims_csv_export_neutralises_formula_injection():
    from eclaim.api.routes import EXPORT_COLUMNS, render_claims_csv

    vendor_i = EXPORT_COLUMNS.index("vendor")
    amount_i = EXPORT_COLUMNS.index("total_amount")
    row = [None] * len(EXPORT_COLUMNS)
    row[vendor_i] = "=cmd|'/c calc'!A1"          # malicious free-text
    row[amount_i] = Decimal("-50.00")            # legitimate negative number
    csv_text = render_claims_csv([row])
    body = csv_text.splitlines()[1]
    assert "'=cmd" in body                        # free-text defused
    assert "-50.00" in body and "'-50.00" not in body   # numeric left intact
