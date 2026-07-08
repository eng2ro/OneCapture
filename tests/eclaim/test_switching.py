"""Document-type switching (Appendix E3): a misfiled page moves between e-Claim
and AP — allowed only PRE-approval, audited both sides, same image provenance,
SoD carrying over to whatever the page becomes.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from eclaim.auth.principal import Principal
from eclaim.db.models import (
    ApInvoice,
    AppUser,
    Claim,
    ClaimLine,
    DocumentIntake,
    Vendor,
)
from eclaim.ocr.base import Extraction
from eclaim.services import ap
from eclaim.services.claims import ClaimService, Repos
from eclaim.services.sod import SoDViolation, check_can_approve


def _upload_bill_as_expense(client, fake_ocr, *, doc_no="INV-SW1"):
    """A vendor bill that slipped into e-Claim as an expense (manual keying or a
    pre-classifier tab) — the case E3 exists to correct."""
    fake_ocr.extraction = Extraction(
        vendor="Bina Jaya Hardware", doc_no=doc_no, total_amount=Decimal("530.00"),
        tax_amount=Decimal("30.00"), currency="MYR", date="26 SEP 2025",
        expense_type="other", quantity=Decimal("200"), unit="L",
    )
    files = {"file": ("bill.png", b"\x89PNG switch " + doc_no.encode(), "image/png")}
    return client.post("/api/claims/upload", files=files,
                       data={"attested": "true"}).json()["id"]


# --------------------------------------------------------------------------- #
# Claim line → vendor bill
# --------------------------------------------------------------------------- #
def test_switch_line_moves_page_to_holding_with_fields_and_provenance(client, fake_ocr, db_session):
    cid = _upload_bill_as_expense(client, fake_ocr)
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()
    sha = line.image_sha256

    r = client.post(f"/claims/{cid}/lines/{line.id}/to-vendor-bill",
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/intake/holding")

    intake = db_session.execute(select(DocumentIntake)).scalars().one()
    assert intake.image_sha256 == sha                    # same evidence, no re-upload
    assert intake.document_type == "vendor_invoice"
    assert intake.routed_by == "user" and intake.status == "open"
    assert intake.vendor == "Bina Jaya Hardware"
    assert intake.doc_no == "INV-SW1"
    assert intake.total_amount == Decimal("530.00")
    assert intake.quantity == Decimal("200")             # the litres survive the switch
    assert intake.tax_amount == Decimal("30.00")

    db_session.expire_all()
    claim = db_session.get(Claim, uuid.UUID(cid))
    assert claim.status == "rejected"                    # only page left → voided
    assert "switched" in (claim.approver_note or "")
    events = client.get(f"/api/audit/{cid}").json()
    assert any(e["event_type"] == "line_switched_to_ap" for e in events)


def test_switch_is_locked_after_approval_and_idempotent(client, fake_ocr, db_session):
    cid = _upload_bill_as_expense(client, fake_ocr, doc_no="INV-SW2")
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200

    r = client.post(f"/claims/{cid}/lines/{line.id}/to-vendor-bill",
                    follow_redirects=False)
    assert r.status_code == 409                          # post-approval lock
    assert "reject/reversal" in r.json()["detail"]
    assert db_session.execute(select(DocumentIntake)).scalars().first() is None

    # Pre-approval double-click: the second switch finds no line — no second intake.
    cid2 = _upload_bill_as_expense(client, fake_ocr, doc_no="INV-SW3")
    line2 = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid2))
    ).scalars().one()
    assert client.post(f"/claims/{cid2}/lines/{line2.id}/to-vendor-bill",
                       follow_redirects=False).status_code == 303
    assert client.post(f"/claims/{cid2}/lines/{line2.id}/to-vendor-bill",
                       follow_redirects=False).status_code == 409
    intakes = db_session.execute(select(DocumentIntake)).scalars().all()
    assert len(intakes) == 1


def test_batch_switch_moves_many_lines_at_once(client, fake_ocr, db_session):
    """The supplier-invoice-dump case (screenshot: 24 vendor lines filed as one
    expense claim): tick lines and move them all to Vendor bills in one action,
    not 24 clicks-into-modals. The claim voids when its last page leaves."""
    from eclaim.services.claims import ClaimService, Repos

    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    # Build one claim with three vendor-invoice-looking lines.
    claim = svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])
    line_ids = []
    for i in range(3):
        fake_ocr.extraction = Extraction(
            vendor="Lucky Frozen Sdn Bhd", doc_no=f"J826-{i}",
            total_amount=Decimal("100.00") + i, expense_type="other",
        )
        ln = svc.add_line(
            repos=repos, claim=claim, image_bytes=b"\x89PNG dump " + bytes([i]),
            media_type="image/png", ocr=fake_ocr, image_dir=_img_dir(),
            payment_method="corporate_card",
        )
        line_ids.append(ln.id)
    svc.submit(repos=repos, claim=claim, actor="t", line_count=3, attested=False)
    db_session.commit()

    r = client.post(f"/claims/{claim.id}/lines/to-vendor-bills",
                    data={"line_ids": [str(lid) for lid in line_ids[:2]]},
                    follow_redirects=False)                  # move 2 of the 3
    assert r.status_code == 303 and r.headers["location"].endswith("/review")

    db_session.expire_all()
    from eclaim.db.models import Claim, DocumentIntake

    assert len(db_session.execute(select(ClaimLine).where(
        ClaimLine.claim_id == claim.id)).scalars().all()) == 1     # one line left
    assert len(db_session.execute(select(DocumentIntake)).scalars().all()) == 2
    assert db_session.get(Claim, claim.id).status == "in_review"   # not voided (1 left)

    # Move the last one → the claim voids and the redirect goes to the queue.
    r2 = client.post(f"/claims/{claim.id}/lines/to-vendor-bills",
                     data={"line_ids": [str(line_ids[2])]}, follow_redirects=False)
    assert r2.headers["location"].startswith("/intake/holding")
    db_session.expire_all()
    assert db_session.get(Claim, claim.id).status == "rejected"
    assert len(db_session.execute(select(DocumentIntake)).scalars().all()) == 3


def _img_dir():
    import tempfile
    from pathlib import Path
    d = Path(tempfile.gettempdir()) / "oc-switch-test"
    d.mkdir(exist_ok=True)
    return d


def test_mileage_line_cannot_switch(client, db_session):
    r = client.post("/capture/mileage", data={
        "origin": "KL", "destination": "Ipoh", "trip_date": "2026-07-04",
        "attested": "yes", "vehicle_id": "",
    }, follow_redirects=False)
    cid = r.headers["location"].split("/claims/")[1].split("/")[0]
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()
    resp = client.post(f"/claims/{cid}/lines/{line.id}/to-vendor-bill",
                       follow_redirects=False)
    assert resp.status_code == 409
    assert "mileage" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# AP invoice → staff expense
# --------------------------------------------------------------------------- #
def _filed_invoice(db_session, ids, *, doc_no="INV-SW9"):
    intake = DocumentIntake(
        firm_id=ids["firm"], client_id=ids["client"], created_by_user_id=ids["user"],
        document_type="vendor_invoice", routed_to="ap_holding",
        vendor="Grab Receipts Sdn", doc_no=doc_no, total_amount=Decimal("48.00"),
        currency="MYR", type_signals=[], quantity=None, unit=None,
        image_sha256=f"sha-{doc_no}", image_path=f"/img/{doc_no}.png",
    )
    db_session.add(intake)
    db_session.flush()
    return ap.create_from_intake(db_session, intake=intake, actor="t")


def test_switch_invoice_creates_review_claim_and_rejects_the_bill(client, db_session):
    ids = db_session.info["principal"]
    inv = _filed_invoice(db_session, ids)
    db_session.commit()

    r = client.post(f"/ap/{inv.id}/to-expense", follow_redirects=False)
    assert r.status_code == 303 and "/claims/" in r.headers["location"]
    cid = r.headers["location"].split("/claims/")[1].split("/")[0]

    db_session.expire_all()
    claim = db_session.get(Claim, uuid.UUID(cid))
    assert claim.status == "in_review"
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == claim.id)
    ).scalars().one()
    assert line.vendor == "Grab Receipts Sdn"
    assert line.total_amount == Decimal("48.00")
    assert line.image_sha256 == f"sha-{inv.doc_no}"      # same evidence

    assert db_session.get(ApInvoice, inv.id).status == "rejected"
    events = client.get(f"/api/audit/{cid}").json()
    assert any(e["event_type"] == "converted_from_ap" for e in events)

    # Idempotent / locked: a rejected invoice cannot switch again.
    assert client.post(f"/ap/{inv.id}/to-expense",
                       follow_redirects=False).status_code == 409


# --------------------------------------------------------------------------- #
# Holding-queue page → back INTO the claim under review (no vendor-module trip)
# --------------------------------------------------------------------------- #
def _open_diverted(db_session, ids, *, doc_no="INV-BB1", created_by=None):
    row = DocumentIntake(
        firm_id=ids["firm"], client_id=ids["client"],
        created_by_user_id=(created_by or ids["user"]),
        document_type="vendor_invoice", routed_to="ap_holding", status="open",
        vendor="Kedai Runcit", doc_no=doc_no, total_amount=Decimal("42.00"),
        currency="MYR", type_signals=[], quantity=Decimal("5"), unit="L",
        tax_amount=Decimal("2.00"),
        image_sha256=f"sha-{doc_no}", image_path=f"/img/{doc_no}.png",
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_bring_back_appends_the_page_to_the_same_claim(client, fake_ocr, db_session):
    """Owner request: a mis-diverted receipt returns to the claim the user is
    ALREADY reviewing — one click on the review page, no vendor-bills detour,
    no new claim, no re-OCR (the stored read + image come along)."""
    ids = db_session.info["principal"]
    cid = _upload_bill_as_expense(client, fake_ocr, doc_no="INV-BASE")
    intake = _open_diverted(db_session, ids)
    db_session.commit()

    page = client.get(f"/claims/{cid}/review")
    assert "Bring back into this claim" in page.text     # the affordance is THERE

    r = client.post(f"/intake/{intake.id}/reroute",
                    data={"to": "eclaim", "claim_id": cid}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/claims/{cid}/review"   # straight back

    db_session.expire_all()
    lines = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
        .order_by(ClaimLine.line_no)
    ).scalars().all()
    assert len(lines) == 2                               # appended, not a new claim
    back = lines[-1]
    assert back.vendor == "Kedai Runcit"
    assert back.total_amount == Decimal("42.00")
    assert back.quantity == Decimal("5") and back.unit == "L"
    assert back.tax_amount == Decimal("2.00")
    assert back.image_sha256 == "sha-INV-BB1"            # same evidence

    row = db_session.get(DocumentIntake, intake.id)
    assert row.status == "consumed" and row.claim_id == uuid.UUID(cid)
    events = client.get(f"/api/audit/{cid}").json()
    assert any(
        e["event_type"] == "edited"
        and (e.get("detail") or {}).get("added") == "line_from_intake"
        for e in events
    )


def test_bring_back_locked_after_approval(client, fake_ocr, db_session):
    ids = db_session.info["principal"]
    cid = _upload_bill_as_expense(client, fake_ocr, doc_no="INV-BB2")
    intake = _open_diverted(db_session, ids, doc_no="INV-BB3")
    db_session.commit()
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200

    r = client.post(f"/intake/{intake.id}/reroute",
                    data={"to": "eclaim", "claim_id": cid}, follow_redirects=False)
    assert r.status_code == 400
    db_session.expire_all()
    assert db_session.get(DocumentIntake, intake.id).status == "open"   # untouched
    # And the approved claim shows no bring-back panel.
    assert "Bring back into this claim" not in client.get(f"/claims/{cid}/review").text


def test_bring_back_lists_only_the_users_own_pages(client, fake_ocr, db_session):
    ids = db_session.info["principal"]
    other = AppUser(firm_id=ids["firm"], email="someone-else@seed.test",
                    display_name="o", base_role="approver")
    db_session.add(other)
    db_session.flush()
    cid = _upload_bill_as_expense(client, fake_ocr, doc_no="INV-BB4")
    _open_diverted(db_session, ids, doc_no="INV-OTHER", created_by=other.id)
    db_session.commit()

    page = client.get(f"/claims/{cid}/review")
    assert "INV-OTHER" not in page.text                  # someone else's page
    assert "Bring back into this claim" not in page.text


def test_switcher_cannot_approve_the_converted_claim(client, db_session):
    """SoD carryover: switching is a MAKER action — the converter is the claim's
    creator, so the approve gate blocks them."""
    ids = db_session.info["principal"]
    inv = _filed_invoice(db_session, ids, doc_no="INV-SOD")
    editor_user = AppUser(firm_id=ids["firm"], email="switcher@seed.test",
                          display_name="s", base_role="partner")
    db_session.add(editor_user)
    db_session.flush()
    editor = Principal(user_id=editor_user.id, firm_id=ids["firm"], base_role="partner",
                       allowed_client_ids=frozenset({ids["client"]}),
                       email="switcher@seed.test")
    claim = ap.switch_to_expense(db_session, invoice_id=inv.id, editor=editor, actor="s")
    assert claim.created_by_user_id == editor.user_id
    with pytest.raises(SoDViolation):
        check_can_approve(claim, editor)
