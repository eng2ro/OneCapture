"""Approvals inbox (Appendix E1/E2): one workspace, two tabs, live counts."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from eclaim.db.models import Category, DocumentIntake
from eclaim.ocr.base import Extraction
from eclaim.services import ap


def _claim_in_review(client, fake_ocr):
    fake_ocr.extraction = Extraction(
        vendor="Kedai Kopi", total_amount=Decimal("25.00"), expense_type="other",
    )
    files = {"file": ("r.png", b"\x89PNG inbox", "image/png")}
    return client.post("/api/claims/upload", files=files,
                       data={"attested": "true"}).json()["id"]


def _pending_ap(db_session, ids):
    from eclaim.auth.principal import Principal
    from eclaim.db.models import AppUser

    intake = DocumentIntake(
        firm_id=ids["firm"], client_id=ids["client"], created_by_user_id=ids["user"],
        document_type="vendor_invoice", routed_to="ap_holding",
        vendor="Bina Jaya", doc_no="INV-INBOX", total_amount=Decimal("900"),
        currency="MYR", type_signals=[],
    )
    db_session.add(intake)
    db_session.flush()
    inv = ap.create_from_intake(db_session, intake=intake, actor="t")
    coder_user = AppUser(firm_id=ids["firm"], email="inbox-coder@seed.test",
                         display_name="c", base_role="partner")
    db_session.add(coder_user)
    db_session.flush()
    coder = Principal(user_id=coder_user.id, firm_id=ids["firm"], base_role="partner",
                      allowed_client_ids=frozenset({ids["client"]}),
                      email="inbox-coder@seed.test")
    cat = db_session.execute(
        select(Category).where(Category.client_id == ids["client"]).limit(1)
    ).scalars().one()
    ap.code_line(db_session, line_id=ap.lines(db_session, inv.id)[0].id,
                 coder=coder, actor="c", gl_code="6000", category_id=cat.id)
    return inv


def test_inbox_shows_both_tabs_with_counts_and_rows(client, fake_ocr, db_session):
    ids = db_session.info["principal"]
    cid = _claim_in_review(client, fake_ocr)
    inv = _pending_ap(db_session, ids)
    db_session.add(DocumentIntake(                       # a "needs a check" page
        firm_id=ids["firm"], client_id=ids["client"], document_type="unknown",
        routed_to="pending", vendor="Mystery Shop", type_signals=[],
    ))
    db_session.commit()

    page = client.get("/approvals")
    assert page.status_code == 200
    assert "Staff expenses" in page.text and "Vendor bills" in page.text
    assert f"/claims/{cid}/review" in page.text          # expense row links to review

    vendor_tab = client.get("/approvals?tab=vendor")
    assert f"/ap/{inv.id}" in vendor_tab.text            # bill row links to AP detail
    assert "Bina Jaya" in vendor_tab.text
    assert "Mystery Shop" in vendor_tab.text             # needs-a-check section
    assert "needs a check" in vendor_tab.text.lower()


def test_nav_badge_counts_claims_plus_ap(client, fake_ocr, db_session):
    ids = db_session.info["principal"]
    _claim_in_review(client, fake_ocr)
    _pending_ap(db_session, ids)
    db_session.commit()
    page = client.get("/claims")
    assert 'href="/approvals"' in page.text              # nav entry present
    # badge total = 1 in-review claim + 1 coded/pending AP bill
    assert ">2</span>" in page.text


def test_document_type_pills_render(client, fake_ocr, db_session):
    """E2: the reviewer always sees WHICH kind of document they are looking at."""
    ids = db_session.info["principal"]
    cid = _claim_in_review(client, fake_ocr)
    inv = _pending_ap(db_session, ids)
    db_session.commit()
    assert "staff expense" in client.get(f"/claims/{cid}/review").text
    assert "vendor bill" in client.get(f"/ap/{inv.id}").text
