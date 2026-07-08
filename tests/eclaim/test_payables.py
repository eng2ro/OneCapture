"""Payables overview: the reimburse-staff and pay-vendors totals in one place."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from eclaim.auth.principal import Principal
from eclaim.db.models import ApInvoice, AppUser, Claim, DocumentIntake
from eclaim.services import ap
from eclaim.services import payables as payables_service

def _cat_id(db_session):
    """Any seeded category for this client — the coding gate requires every AP line
    to carry an explicit category before submit/approve (F-E item 13)."""
    from sqlalchemy import select as _sel

    from eclaim.db.models import Category as _Cat
    ids = db_session.info["principal"]
    return db_session.execute(
        _sel(_Cat.id).where(_Cat.client_id == ids["client"]).limit(1)
    ).scalar_one()



# --- staff reimbursement (an approved out-of-pocket claim) ------------------ #
def _approved_out_of_pocket_claim(client, amount="100"):
    from decimal import Decimal as _D

    # A single out-of-pocket receipt via the API, attested, then approved.
    import eclaim.api.deps as deps  # noqa: F401 (kept explicit for clarity)
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    cid = client.post("/api/claims/upload", files=files, data={"attested": "true"}).json()["id"]
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    return cid


def _released_out_of_pocket_claim(client, amount="100"):
    """Approved AND released — the only state a claim may be marked paid from
    (payment after release keeps the attestation gate + CarbonNext handoff intact)."""
    cid = _approved_out_of_pocket_claim(client, amount=amount)
    assert client.post(f"/api/claims/{cid}/release").status_code == 200
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


def _approved_invoice(db_session, ids, total="300", filer_id=None):
    # The filer defaults to a DIFFERENT user than the test principal: the settlement
    # SoD rule (filer may not record the payment) would otherwise block the pay step.
    if filer_id is None:
        filer_id = _user(db_session, ids, f"filer-{uuid.uuid4().hex[:6]}@seed.test").id
    intake = DocumentIntake(
        firm_id=ids["firm"], client_id=ids["client"], created_by_user_id=filer_id,
        document_type="vendor_invoice", routed_to="ap_holding",
        vendor="Acme", doc_no="INV-PAY", total_amount=Decimal(total), currency="MYR",
        type_signals=[],
    )
    db_session.add(intake)
    db_session.flush()
    inv = ap.create_from_intake(db_session, intake=intake, actor="t")
    coder = _principal(ids, _user(db_session, ids, "pc@seed.test").id, "pc@seed.test")
    approver = _principal(ids, _user(db_session, ids, "pa@seed.test").id, "pa@seed.test")
    ap.code_line(db_session, line_id=ap.lines(db_session, inv.id)[0].id, coder=coder, actor="pc", gl_code="6000", category_id=_cat_id(db_session))
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
    cid = _released_out_of_pocket_claim(client)
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


def test_claim_cannot_be_paid_before_release(client, db_session):
    """Paying an approved-but-unreleased claim would permanently strand its CarbonNext
    handoff (release refuses 'paid') and bypass the attestation gate — blocked."""
    cid = _approved_out_of_pocket_claim(client)
    db_session.commit()
    r = client.post("/payables/pay", data={"kind": "claim", "id": cid},
                    follow_redirects=False)
    assert r.status_code == 409
    assert "release" in r.json()["detail"].lower()
    db_session.expire_all()
    assert db_session.get(Claim, uuid.UUID(cid)).status == "approved"   # untouched


def test_payables_page_gates_pay_button_on_release(client, db_session):
    """An approved claim shows 'release first', not a pay button; a released one pays."""
    cid = _approved_out_of_pocket_claim(client)
    db_session.commit()
    page = client.get("/payables")
    assert "release first" in page.text
    assert client.post(f"/api/claims/{cid}/release").status_code == 200
    db_session.commit()
    page = client.get("/payables")
    assert "Mark this reimbursement paid" in page.text


def test_creator_cannot_pay_own_claim(client, db_session):
    """Settlement SoD: the user who keyed the claim may not record its payment."""
    ids = db_session.info["principal"]
    cid = _released_out_of_pocket_claim(client)
    claim = db_session.get(Claim, uuid.UUID(cid))
    # Make the test principal the MAKER. The claim was approved by the same test
    # principal via the API, and ck_claim_sod forbids creator==approver — so move
    # the approval to a different user first; the payer==creator rule is what's
    # under test here.
    other = _user(db_session, ids, "other-approver@seed.test")
    claim.approved_by_user_id = other.id
    claim.created_by_user_id = ids["user"]
    db_session.commit()
    r = client.post("/payables/pay", data={"kind": "claim", "id": cid},
                    follow_redirects=False)
    assert r.status_code == 403
    db_session.expire_all()
    assert db_session.get(Claim, uuid.UUID(cid)).status != "paid"


def test_filer_cannot_pay_own_ap_invoice(client, db_session):
    """Settlement SoD on the AP side: the filer may not record the payment."""
    ids = db_session.info["principal"]
    inv = _approved_invoice(db_session, ids, total="300")
    inv.created_by_user_id = ids["user"]        # test principal filed it
    db_session.commit()
    r = client.post("/payables/pay", data={"kind": "ap", "id": str(inv.id)},
                    follow_redirects=False)
    assert r.status_code == 403
    db_session.expire_all()
    assert db_session.get(ApInvoice, inv.id).status == "approved"


def test_paid_invoice_stays_exportable_until_posted(client, db_session):
    """H2: paying before ERP posting must not drop the bill out of the CSV pipeline.
    A paid, unposted invoice stays in the export; posting stamps the ERP key while
    keeping the terminal 'paid' status; then it leaves the export."""
    from eclaim.services import erp as erp_service

    ids = db_session.info["principal"]
    inv = _approved_invoice(db_session, ids, total="300")
    ap.mark_paid(db_session, invoice_id=inv.id, actor="finance")
    db_session.flush()

    exportable = ap.exportable_invoices(db_session, frozenset({ids["client"]}))
    assert inv.id in {i.id for i in exportable}

    result = erp_service.ManualCsvConnector().push_ap_invoice(inv)
    erp_service.mark_posted(db_session, inv, result)
    assert inv.status == "paid"                  # terminal status preserved
    assert inv.erp_doc_entry is not None         # but the ERP key is stamped
    exportable = ap.exportable_invoices(db_session, frozenset({ids["client"]}))
    assert inv.id not in {i.id for i in exportable}


def test_mixed_currencies_are_not_summed_into_one_rm_figure(client, db_session):
    """A USD invoice must not be silently added into an 'RM' total."""
    ids = db_session.info["principal"]
    _approved_invoice(db_session, ids, total="300")
    intake = DocumentIntake(
        firm_id=ids["firm"], client_id=ids["client"], created_by_user_id=ids["user"],
        document_type="vendor_invoice", routed_to="ap_holding",
        vendor="Acme US", doc_no="INV-USD", total_amount=Decimal("100"), currency="USD",
        type_signals=[],
    )
    db_session.add(intake)
    db_session.flush()
    inv2 = ap.create_from_intake(db_session, intake=intake, actor="t")
    coder = _principal(ids, _user(db_session, ids, "pc2@seed.test").id, "pc2@seed.test")
    approver = _principal(ids, _user(db_session, ids, "pa2@seed.test").id, "pa2@seed.test")
    ap.code_line(db_session, line_id=ap.lines(db_session, inv2.id)[0].id, coder=coder, actor="pc", gl_code="6000", category_id=_cat_id(db_session))
    ap.approve(db_session, invoice_id=inv2.id, approver=approver, actor="pa")

    p = payables_service.payables(db_session, frozenset({ids["client"]}))
    assert p.pay_by_ccy["MYR"] == Decimal("300") and p.pay_by_ccy["USD"] == Decimal("100")
    assert "RM 300.00" in p.pay_display and "USD 100.00" in p.pay_display
    assert "RM 400.00" not in p.pay_display      # never a mixed-currency sum


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
