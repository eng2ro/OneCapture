"""Payables overview: the reimburse-staff and pay-vendors totals in one place."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from eclaim.auth.principal import Principal
from eclaim.db.models import ApInvoice, AppUser, Claim, DocumentIntake
from eclaim.services import ap
from eclaim.services import payables as payables_service


# --- staff reimbursement (an approved out-of-pocket claim) ------------------ #
def _approved_out_of_pocket_claim(client, amount="100"):
    from decimal import Decimal as _D

    # A single out-of-pocket receipt via the API, attested, then approved.
    import eclaim.api.deps as deps  # noqa: F401 (kept explicit for clarity)
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    cid = client.post("/api/claims/upload", files=files, data={"attested": "true"}).json()["id"]
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    return cid


# --- vendor bill (an approved AP invoice) ----------------------------------- #
def _user(db_session, ids, email):
    u = AppUser(firm_id=ids["firm"], email=email, display_name=email, base_role="partner")
    db_session.add(u)
    db_session.flush()
    return u


def _principal(ids, uid, email):
    return Principal(
        user_id=uid, firm_id=ids["firm"], base_role="partner",
        allowed_client_ids=frozenset({ids["client"]}), email=email,
    )


def _approved_invoice(db_session, ids, total="300"):
    intake = DocumentIntake(
        firm_id=ids["firm"], client_id=ids["client"], created_by_user_id=ids["user"],
        document_type="vendor_invoice", routed_to="ap_holding",
        vendor="Acme", doc_no="INV-PAY", total_amount=Decimal(total), currency="MYR",
        type_signals=[],
    )
    db_session.add(intake)
    db_session.flush()
    inv = ap.create_from_intake(db_session, intake=intake, actor="t")
    coder = _principal(ids, _user(db_session, ids, "pc@seed.test").id, "pc@seed.test")
    approver = _principal(ids, _user(db_session, ids, "pa@seed.test").id, "pa@seed.test")
    ap.code_line(db_session, line_id=ap.lines(db_session, inv.id)[0].id, coder=coder, actor="pc", gl_code="6000")
    ap.approve(db_session, invoice_id=inv.id, approver=approver, actor="pa")
    return inv


def test_payables_totals_the_two_pots_separately(client, db_session):
    ids = db_session.info["principal"]
    _approved_out_of_pocket_claim(client, amount="100")
    _approved_invoice(db_session, ids, total="300")

    p = payables_service.payables(db_session, frozenset({ids["client"]}))
    assert p.reimburse_total == Decimal("100.00") and p.reimburse_count == 1
    assert p.pay_total == Decimal("300.00") and p.pay_count == 1


def test_payables_excludes_paid_and_pre_approval(client, db_session):
    """Only committed, still-owed amounts count — a captured (uncoded) invoice and a
    still-in-review claim are NOT payables yet."""
    ids = db_session.info["principal"]
    # A captured (not approved) invoice — not yet a payable.
    intake = DocumentIntake(
        firm_id=ids["firm"], client_id=ids["client"], document_type="vendor_invoice",
        routed_to="ap_holding", vendor="X", doc_no="CAP", total_amount=Decimal("50"),
        currency="MYR", type_signals=[],
    )
    db_session.add(intake)
    db_session.flush()
    ap.create_from_intake(db_session, intake=intake, actor="t")   # status 'captured'

    p = payables_service.payables(db_session, frozenset({ids["client"]}))
    assert p.pay_total == Decimal("0") and p.pay_count == 0
    assert p.reimburse_total == Decimal("0") and p.reimburse_count == 0


def test_payables_page_shows_both_totals_and_grand_total(client, db_session):
    ids = db_session.info["principal"]
    _approved_out_of_pocket_claim(client, amount="100")
    _approved_invoice(db_session, ids, total="300")
    db_session.commit()

    page = client.get("/payables")
    assert page.status_code == 200
    assert "Reimburse staff" in page.text and "Pay vendors" in page.text
    assert "RM 100.00" in page.text and "RM 300.00" in page.text
    assert "RM 400.00" in page.text                 # combined grand total


def test_mark_paid_drops_items_off_payables(client, db_session):
    ids = db_session.info["principal"]
    cid = _approved_out_of_pocket_claim(client)
    inv = _approved_invoice(db_session, ids, total="300")
    db_session.commit()

    assert client.post("/payables/pay", data={"kind": "claim", "id": cid},
                       follow_redirects=False).status_code == 303
    assert client.post("/payables/pay", data={"kind": "ap", "id": str(inv.id)},
                       follow_redirects=False).status_code == 303

    db_session.expire_all()
    assert db_session.get(Claim, uuid.UUID(cid)).status == "paid"
    assert db_session.get(ApInvoice, inv.id).status == "paid"
    p = payables_service.payables(db_session, frozenset({ids["client"]}))
    assert p.reimburse_count == 0 and p.pay_count == 0     # both settled → off the list


def test_ap_mark_paid_requires_approved_or_posted(client, db_session):
    ids = db_session.info["principal"]
    intake = DocumentIntake(
        firm_id=ids["firm"], client_id=ids["client"], document_type="vendor_invoice",
        routed_to="ap_holding", vendor="X", doc_no="CAP2", total_amount=Decimal("50"),
        currency="MYR", type_signals=[],
    )
    db_session.add(intake)
    db_session.flush()
    inv = ap.create_from_intake(db_session, intake=intake, actor="t")   # 'captured'
    with pytest.raises(ap.IllegalApTransition):
        ap.mark_paid(db_session, invoice_id=inv.id, actor="x")


def test_claim_mark_paid_rejects_pre_approval(client, db_session):
    """An in-review claim isn't a settled payable yet — can't be marked paid."""
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    cid = client.post("/api/claims/upload", files=files, data={"attested": "true"}).json()["id"]
    # not approved → still in_review
    r = client.post("/payables/pay", data={"kind": "claim", "id": cid}, follow_redirects=False)
    assert r.status_code == 409
