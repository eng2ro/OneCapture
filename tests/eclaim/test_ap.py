"""Accounts-payable domain (C2): vendor bills finance pays.

Pins the workflow (capture → code → approve), the HARD duplicate guard, separation
of duties (coder ≠ approver, DB + service), the module-scoped approval matrix, and
the manual CSV export stub.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from eclaim.auth.principal import Principal
from eclaim.db.models import (
    ApInvoice,
    ApprovalMatrixRule,
    AppUser,
    DocumentIntake,
)
from eclaim.services import ap as ap
from eclaim.services import erp as erp
from eclaim.services.claims import Repos
from eclaim.services.sod import SoDViolation


def _user(db_session, ids, email) -> AppUser:
    u = AppUser(firm_id=ids["firm"], email=email, display_name=email, base_role="partner")
    db_session.add(u)
    db_session.flush()
    return u


def _principal(ids, user_id, role="partner", email="p@seed.test") -> Principal:
    return Principal(
        user_id=user_id, firm_id=ids["firm"], base_role=role,
        allowed_client_ids=frozenset({ids["client"]}), email=email,
    )


def _intake(db_session, ids, *, vendor="Acme Supplies", doc_no="INV-1", total="300") -> DocumentIntake:
    row = DocumentIntake(
        firm_id=ids["firm"], client_id=ids["client"], created_by_user_id=ids["user"],
        document_type="vendor_invoice", routed_to="ap_holding",
        vendor=vendor, doc_no=doc_no, total_amount=Decimal(total), currency="MYR",
        type_signals=[],
    )
    db_session.add(row)
    db_session.flush()
    return row


# --------------------------------------------------------------------------- #
# Capture from a diverted vendor bill
# --------------------------------------------------------------------------- #
def test_create_from_intake_builds_invoice_and_consumes_it(client, db_session):
    ids = db_session.info["principal"]
    intake = _intake(db_session, ids)
    inv = ap.create_from_intake(db_session, intake=intake, actor="t")

    assert inv.status == "captured"
    assert inv.total_amount == Decimal("300")
    assert inv.doc_no == "INV-1"
    assert ap.lines(db_session, inv.id)                      # a seeded line
    assert db_session.get(DocumentIntake, intake.id).status == "consumed"
    chain = Repos.for_session(db_session).audit.chain("ap_invoice", inv.id)
    assert any(e.event_type == "ap_captured" for e in chain)


# --------------------------------------------------------------------------- #
# Hard duplicate detection
# --------------------------------------------------------------------------- #
def test_quotation_intake_cannot_be_filed_as_ap_invoice(client, db_session):
    """A quotation / PO / DO is not a payable bill — create_from_intake refuses it."""
    ids = db_session.info["principal"]
    for dt in ("quotation", "purchase_order", "delivery_order"):
        intake = _intake(db_session, ids, doc_no=f"Q-{dt}")
        intake.document_type = dt
        db_session.flush()
        with pytest.raises(ap.ApError, match="not a payable bill"):
            ap.create_from_intake(db_session, intake=intake, actor="t")


def test_same_vendor_docno_amount_is_held_as_duplicate(client, db_session):
    ids = db_session.info["principal"]
    first = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    second = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    assert first.status == "captured"
    assert second.status == "held"
    assert "duplicate" in (second.hold_reason or "").lower()


def test_release_hold_lets_a_false_positive_proceed(client, db_session):
    """F6: a duplicate hold is no longer a dead end — releasing the hold returns the
    bill to the normal flow (not just reject), audited."""
    ids = db_session.info["principal"]
    ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    dup = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    assert dup.status == "held"

    released = ap.release_hold(db_session, invoice_id=dup.id, actor="mgr")
    assert released.status == "captured" and released.hold_reason is None
    chain = Repos.for_session(db_session).audit.chain("ap_invoice", dup.id)
    assert any(e.event_type == "ap_hold_released" for e in chain)

    # and it now flows through to approval instead of dead-ending.
    coder = _principal(ids, _user(db_session, ids, "hc@seed.test").id, email="hc@seed.test")
    approver = _principal(ids, _user(db_session, ids, "ha@seed.test").id, email="ha@seed.test")
    ap.code_line(db_session, line_id=ap.lines(db_session, dup.id)[0].id, coder=coder, actor="hc")
    ap.submit_for_approval(db_session, invoice_id=dup.id, actor="hc")
    ap.approve(db_session, invoice_id=dup.id, approver=approver, actor="ha")
    assert db_session.get(ApInvoice, dup.id).status == "approved"


def test_release_hold_only_applies_to_held(client, db_session):
    ids = db_session.info["principal"]
    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")   # captured
    with pytest.raises(ap.IllegalApTransition):
        ap.release_hold(db_session, invoice_id=inv.id, actor="x")


def test_web_release_hold_action(client, db_session):
    ids = db_session.info["principal"]
    ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    dup = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    db_session.commit()

    assert "Not a duplicate" in client.get(f"/ap/{dup.id}").text
    r = client.post(f"/ap/{dup.id}/release-hold", follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(ApInvoice, dup.id).status == "captured"


def test_different_amount_is_not_a_duplicate(client, db_session):
    ids = db_session.info["principal"]
    ap.create_from_intake(db_session, intake=_intake(db_session, ids, total="300"), actor="t")
    other = ap.create_from_intake(
        db_session, intake=_intake(db_session, ids, total="999"), actor="t"
    )
    assert other.status == "captured"       # same vendor+doc_no but different amount


# --------------------------------------------------------------------------- #
# Coding + separation of duties
# --------------------------------------------------------------------------- #
def test_code_then_approve_by_a_different_user(client, db_session):
    ids = db_session.info["principal"]
    coder = _principal(ids, _user(db_session, ids, "coder@seed.test").id, email="coder@seed.test")
    approver = _principal(ids, _user(db_session, ids, "boss@seed.test").id, email="boss@seed.test")

    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    line = ap.lines(db_session, inv.id)[0]
    ap.code_line(db_session, line_id=line.id, coder=coder, actor="coder", gl_code="6000")
    assert db_session.get(ApInvoice, inv.id).status == "coded"

    ap.approve(db_session, invoice_id=inv.id, approver=approver, actor="boss")
    fresh = db_session.get(ApInvoice, inv.id)
    assert fresh.status == "approved" and fresh.approved_by_user_id == approver.user_id


def test_coder_cannot_approve_their_own_invoice(client, db_session):
    ids = db_session.info["principal"]
    coder = _principal(ids, _user(db_session, ids, "self@seed.test").id, email="self@seed.test")
    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    line = ap.lines(db_session, inv.id)[0]
    ap.code_line(db_session, line_id=line.id, coder=coder, actor="self", gl_code="6000")

    with pytest.raises(SoDViolation):
        ap.approve(db_session, invoice_id=inv.id, approver=coder, actor="self")
    # the blocked attempt is audited, and the invoice is NOT approved
    assert db_session.get(ApInvoice, inv.id).status == "coded"
    chain = Repos.for_session(db_session).audit.chain("ap_invoice", inv.id)
    assert any(e.event_type == "ap_approval_denied" for e in chain)


def test_denied_approval_written_in_own_txn_not_request_session(client, db_session, monkeypatch):
    """B5 contract for AP: a blocked approval writes ``ap_approval_denied`` in its OWN
    short-lived transaction — never by committing the request session (which the route
    rolls back on the 403). Spy on the request session's commit: it must stay untouched
    while the denial is still durably written. Fails if ap.py reverts to writing the
    event on the request session (it would be lost on the route rollback)."""
    ids = db_session.info["principal"]
    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    seed = _principal(ids, ids["user"], email="partner@seed.test")
    ap.code_line(db_session, line_id=ap.lines(db_session, inv.id)[0].id, coder=seed, actor="c")

    commits: list[int] = []
    monkeypatch.setattr(db_session, "commit", lambda: commits.append(1))
    with pytest.raises(SoDViolation):
        ap.approve(db_session, invoice_id=inv.id, approver=seed, actor="partner@seed.test")

    assert commits == [], "the denial must not commit the request session (blocker B5)"
    chain = Repos.for_session(db_session).audit.chain("ap_invoice", inv.id)
    assert any(e.event_type == "ap_approval_denied" for e in chain)
    assert db_session.get(ApInvoice, inv.id).status == "coded"   # not approved


def test_filer_cannot_submit_uncoded_and_self_approve(client, db_session):
    """F5: the user who FILED a bill must not be able to skip coding and approve it
    alone. Submitting requires coded status, and approve refuses an uncoded invoice."""
    ids = db_session.info["principal"]
    filer = _principal(ids, ids["user"], email="partner@seed.test")   # created_by
    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="filer")

    # Can't send an UNCODED (captured) invoice for approval.
    with pytest.raises(ap.IllegalApTransition):
        ap.submit_for_approval(db_session, invoice_id=inv.id, actor="filer")
    # And can't approve a captured (uncoded) invoice.
    with pytest.raises((SoDViolation, ap.IllegalApTransition)):
        ap.approve(db_session, invoice_id=inv.id, approver=filer, actor="filer")
    assert db_session.get(ApInvoice, inv.id).status == "captured"


def test_filer_cannot_approve_even_when_someone_else_codes(client, db_session):
    """F5: separation of duties spans the whole preparation — the FILER can't approve
    even if a different user did the coding."""
    ids = db_session.info["principal"]
    filer = _principal(ids, ids["user"], email="partner@seed.test")   # = created_by
    coder = _principal(ids, _user(db_session, ids, "cx@seed.test").id, email="cx@seed.test")
    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="filer")
    ap.code_line(db_session, line_id=ap.lines(db_session, inv.id)[0].id, coder=coder, actor="cx")

    with pytest.raises(SoDViolation, match="filed"):
        ap.approve(db_session, invoice_id=inv.id, approver=filer, actor="filer")


def test_double_approve_is_rejected(client, db_session):
    ids = db_session.info["principal"]
    coder = _principal(ids, _user(db_session, ids, "cc@seed.test").id, email="cc@seed.test")
    approver = _principal(ids, _user(db_session, ids, "aa@seed.test").id, email="aa@seed.test")
    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    ap.code_line(db_session, line_id=ap.lines(db_session, inv.id)[0].id, coder=coder, actor="cc")
    ap.approve(db_session, invoice_id=inv.id, approver=approver, actor="aa")
    with pytest.raises(ap.IllegalApTransition):
        ap.approve(db_session, invoice_id=inv.id, approver=approver, actor="aa")


def test_duplicate_idempotency_key_is_blocked_by_the_unique(client, db_session):
    """The uq_ap_invoice_idem UNIQUE stops a second insert of the same source document."""
    from sqlalchemy.exc import IntegrityError

    from eclaim.db.models import Vendor
    ids = db_session.info["principal"]
    v = Vendor(firm_id=ids["firm"], client_id=ids["client"], name="Dup Co")
    db_session.add(v)
    db_session.flush()

    def _inv():
        return ApInvoice(
            firm_id=ids["firm"], client_id=ids["client"], vendor_id=v.id,
            doc_no="D1", total_amount=Decimal("10"), idempotency_key="same-key",
        )

    db_session.add(_inv())
    db_session.flush()
    db_session.add(_inv())
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_viewer_cannot_code_or_approve(client, db_session):
    ids = db_session.info["principal"]
    viewer = _principal(ids, ids["user"], role="viewer", email="v@seed.test")
    coder = _principal(ids, _user(db_session, ids, "realcoder@seed.test").id, email="realcoder@seed.test")
    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    line = ap.lines(db_session, inv.id)[0]
    with pytest.raises(SoDViolation):
        ap.code_line(db_session, line_id=line.id, coder=viewer, actor="v", gl_code="6000")
    # Code it for real, then a viewer still cannot approve.
    ap.code_line(db_session, line_id=line.id, coder=coder, actor="c", gl_code="6000")
    with pytest.raises(SoDViolation):
        ap.approve(db_session, invoice_id=inv.id, approver=viewer, actor="v")


def test_db_check_blocks_coder_equals_approver(client, db_session):
    """Defence in depth: even if the service guard were bypassed, the DB CHECK forbids
    the same user as both coder and approver."""
    ids = db_session.info["principal"]
    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    same = ids["user"]
    inv.coded_by_user_id = same
    inv.approved_by_user_id = same
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_db_check_blocks_filer_equals_approver(client, db_session):
    """F5 parity: the widened ck_ap_invoice_sod backs the filer≠approver service rule
    at the database, like e-Claim's ck_claim_sod."""
    ids = db_session.info["principal"]
    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    inv.created_by_user_id = ids["user"]
    inv.coded_by_user_id = _user(db_session, ids, "dbcoder@seed.test").id
    inv.approved_by_user_id = ids["user"]        # approver == filer → DB rejects
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_submitter_is_recorded_and_cannot_approve(client, db_session):
    """F5 residual: the SUBMITTER of a coded invoice is a preparer too — recorded on
    the invoice and barred from approving it, at the service and at the DB."""
    ids = db_session.info["principal"]
    coder = _principal(ids, _user(db_session, ids, "sc@seed.test").id, email="sc@seed.test")
    submitter = _principal(ids, _user(db_session, ids, "ss@seed.test").id, email="ss@seed.test")
    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    ap.code_line(db_session, line_id=ap.lines(db_session, inv.id)[0].id, coder=coder, actor="sc", gl_code="6000")
    ap.submit_for_approval(db_session, invoice_id=inv.id, actor="ss", submitter=submitter)

    assert inv.submitted_by_user_id == submitter.user_id   # recorded
    with pytest.raises(SoDViolation, match="submitted"):
        ap.approve(db_session, invoice_id=inv.id, approver=submitter, actor="ss")
    # And the DB backstop: submitter == approver is rejected even if the guard were bypassed.
    inv.approved_by_user_id = submitter.user_id
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# --------------------------------------------------------------------------- #
# Module-scoped approval matrix
# --------------------------------------------------------------------------- #
def test_ap_matrix_band_requires_partner(client, db_session):
    ids = db_session.info["principal"]
    # An AP-scoped rule: bills >= 100 need a partner.
    db_session.add(ApprovalMatrixRule(
        firm_id=ids["firm"], client_id=ids["client"], step_order=1, approvals_required=1,
        active=True, scope_module="ap", min_amount=Decimal("100"), approver_role="partner",
    ))
    db_session.flush()

    coder = _principal(ids, _user(db_session, ids, "c2@seed.test").id, role="manager", email="c2@seed.test")
    manager = _principal(ids, _user(db_session, ids, "m2@seed.test").id, role="manager", email="m2@seed.test")
    partner = _principal(ids, _user(db_session, ids, "p2@seed.test").id, role="partner", email="p2@seed.test")

    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids, total="500"), actor="t")
    ap.code_line(db_session, line_id=ap.lines(db_session, inv.id)[0].id, coder=coder, actor="c")

    with pytest.raises(SoDViolation):
        ap.approve(db_session, invoice_id=inv.id, approver=manager, actor="m")   # too junior
    ap.approve(db_session, invoice_id=inv.id, approver=partner, actor="p")       # partner ok
    assert db_session.get(ApInvoice, inv.id).status == "approved"


def test_eclaim_scoped_rule_does_not_bind_ap(client, db_session):
    """A rule scoped to the e-Claim module must NOT govern an AP invoice — otherwise the
    scope column is meaningless."""
    ids = db_session.info["principal"]
    db_session.add(ApprovalMatrixRule(
        firm_id=ids["firm"], client_id=ids["client"], step_order=1, approvals_required=1,
        active=True, scope_module="eclaim", min_amount=Decimal("100"), approver_role="partner",
    ))
    db_session.flush()
    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids, total="500"), actor="t")
    assert ap.matrix_rule_for_invoice(db_session, inv) is None    # e-Claim rule doesn't apply


# --------------------------------------------------------------------------- #
# CSV export stub
# --------------------------------------------------------------------------- #
def test_csv_export_of_approved_invoices(client, db_session):
    ids = db_session.info["principal"]
    coder = _principal(ids, _user(db_session, ids, "c3@seed.test").id, email="c3@seed.test")
    approver = _principal(ids, _user(db_session, ids, "a3@seed.test").id, email="a3@seed.test")
    inv = ap.create_from_intake(db_session, intake=_intake(db_session, ids, doc_no="INV-CSV"), actor="t")
    ap.code_line(db_session, line_id=ap.lines(db_session, inv.id)[0].id, coder=coder, actor="c", gl_code="6000")
    ap.approve(db_session, invoice_id=inv.id, approver=approver, actor="a")

    csv_text = erp.export_ap_csv(db_session, [db_session.get(ApInvoice, inv.id)])
    assert "vendor" in csv_text.splitlines()[0]      # header
    assert "INV-CSV" in csv_text
    assert "6000" in csv_text                         # the coded GL


# --------------------------------------------------------------------------- #
# Web surface (thin) — file from holding, list, approve, export
# --------------------------------------------------------------------------- #
def test_file_ap_toctou_collision_is_409(client, db_session):
    """F9: two concurrent file-ap requests on one intake both pass the status check;
    the second collides on the ap_invoice idempotency key. Map that to 409, not a 500.
    Simulated by resetting the intake to 'open' after the first filing."""
    from sqlalchemy import text

    ids = db_session.info["principal"]
    intake = _intake(db_session, ids, doc_no="TOC-1")
    db_session.commit()

    assert client.post(f"/intake/{intake.id}/file-ap", follow_redirects=False).status_code == 303
    # Simulate a racing second request that already passed the consumed-check.
    db_session.execute(
        text("UPDATE document_intake SET status='open' WHERE id=:i"), {"i": str(intake.id)}
    )
    db_session.commit()

    r = client.post(f"/intake/{intake.id}/file-ap", follow_redirects=False)
    assert r.status_code == 409


def test_web_file_ap_from_holding_then_list(client, db_session):
    ids = db_session.info["principal"]
    intake = _intake(db_session, ids, vendor="Widget Co", doc_no="WC-1")
    db_session.commit()

    resp = client.post(f"/intake/{intake.id}/file-ap", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/ap/")

    detail = client.get(resp.headers["location"])          # the AP detail page renders
    assert detail.status_code == 200 and "Widget Co" in detail.text

    page = client.get("/ap")
    assert page.status_code == 200 and "Widget Co" in page.text

    export = client.get("/ap/export.csv")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
