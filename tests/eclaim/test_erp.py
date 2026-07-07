"""ERP export + posting hardening (F8): CSV formula-injection defence, a receipt that
carries the idempotency key, and mark_posted that refuses a wrong status or a double
post."""

from __future__ import annotations

from decimal import Decimal

import pytest

from eclaim.auth.principal import Principal
from eclaim.db.models import ApInvoice, AppUser, Vendor
from eclaim.services import ap, erp


def _user(db_session, ids, email) -> AppUser:
    u = AppUser(firm_id=ids["firm"], email=email, display_name=email, base_role="partner")
    db_session.add(u)
    db_session.flush()
    return u


def _principal(ids, uid, email) -> Principal:
    return Principal(
        user_id=uid, firm_id=ids["firm"], base_role="partner",
        allowed_client_ids=frozenset({ids["client"]}), email=email,
    )


def _invoice(db_session, ids, *, vendor_name="Acme", desc="Consulting", gl="6000", total="300"):
    v = Vendor(firm_id=ids["firm"], client_id=ids["client"], name=vendor_name)
    db_session.add(v)
    db_session.flush()
    return ap.create_invoice(
        db_session, firm_id=ids["firm"], client_id=ids["client"],
        created_by_user_id=ids["user"], vendor_id=v.id, actor="t", doc_no="INV1",
        total_amount=Decimal(total),
        lines=[ap.LineInput(description=desc, line_total=Decimal(total), gl_code=gl)],
    )


def _approved(db_session, ids):
    inv = _invoice(db_session, ids)
    coder = _principal(ids, _user(db_session, ids, "ec@seed.test").id, "ec@seed.test")
    approver = _principal(ids, _user(db_session, ids, "ea@seed.test").id, "ea@seed.test")
    ap.code_line(db_session, line_id=ap.lines(db_session, inv.id)[0].id, coder=coder, actor="ec", gl_code="6000")
    ap.approve(db_session, invoice_id=inv.id, approver=approver, actor="ea")
    return inv


# --------------------------------------------------------------------------- #
# CSV formula injection
# --------------------------------------------------------------------------- #
def test_csv_export_defuses_formula_injection(client, db_session):
    ids = db_session.info["principal"]
    inv = _invoice(
        db_session, ids, vendor_name="=cmd|' /C calc'!A0",
        desc="+SUM(A1)", gl="@evil", total="-300.00",
    )
    csv_text = erp.export_ap_csv(db_session, [db_session.get(ApInvoice, inv.id)])

    assert "'=cmd" in csv_text          # formula-leading text cells are quoted
    assert "'+SUM(A1)" in csv_text
    assert "'@evil" in csv_text
    # a negative numeric keeps its legitimate minus sign — NOT treated as a formula
    assert "-300.00" in csv_text and "'-300" not in csv_text


# --------------------------------------------------------------------------- #
# mark_posted hardening
# --------------------------------------------------------------------------- #
def test_push_receipt_carries_the_idempotency_key(client, db_session):
    ids = db_session.info["principal"]
    inv = _approved(db_session, ids)
    result = erp.ManualCsvConnector().push_ap_invoice(inv)
    assert result.ok and result.idempotency_key == inv.idempotency_key


def test_mark_posted_requires_approved_status(client, db_session):
    ids = db_session.info["principal"]
    inv = _invoice(db_session, ids)          # status 'captured', not approved
    result = erp.ManualCsvConnector().push_ap_invoice(inv)
    with pytest.raises(erp.ErpError):
        erp.mark_posted(db_session, inv, result)
    assert db_session.get(ApInvoice, inv.id).status == "captured"   # unchanged


def test_mark_posted_refuses_double_post(client, db_session):
    ids = db_session.info["principal"]
    inv = _approved(db_session, ids)
    result = erp.ManualCsvConnector().push_ap_invoice(inv)

    erp.mark_posted(db_session, inv, result)
    assert inv.status == "posted" and inv.erp_doc_entry == result.erp_doc_entry

    with pytest.raises(erp.ErpError):        # already carries an erp_doc_entry
        erp.mark_posted(db_session, inv, result)


def test_connector_seam_has_pull_open_pos(client, db_session):
    """F9: the ERPConnector protocol exposes pull_open_pos (for the C4 3-way match);
    the manual stub returns nothing."""
    assert erp.ManualCsvConnector().pull_open_pos(None) == []
