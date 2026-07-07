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
def test_same_vendor_docno_amount_is_held_as_duplicate(client, db_session):
    ids = db_session.info["principal"]
    first = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    second = ap.create_from_intake(db_session, intake=_intake(db_session, ids), actor="t")
    assert first.status == "captured"
    assert second.status == "held"
    assert "duplicate" in (second.hold_reason or "").lower()


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
