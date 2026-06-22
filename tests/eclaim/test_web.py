"""Server-rendered web UI: claims inbox + review page + lifecycle actions.

Exercised through the TestClient (the conftest ``client`` fixture overrides the
principal to a firm partner, so the page can resolve a Principal and the SoD
guard sees a real reviewer). Actions are HTML form POSTs to the web handlers,
which call ClaimService and 303-redirect back.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from eclaim.db.models import Category, Claim, Client, Firm
from eclaim.ocr.base import Extraction


def _upload(client, fake_ocr, extraction: Extraction) -> str:
    fake_ocr.extraction = extraction
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    return client.post("/api/claims/upload", files=files).json()["id"]


def _status(client, cid: str) -> str:
    return client.get(f"/api/claims/{cid}").json()["status"]


_DIESEL = Extraction(expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")


# 1 -------------------------------------------------------------------------
def test_inbox_lists_scoped_claims_and_filters(client, fake_ocr, db_session, db_engine):
    a = _upload(client, fake_ocr, _DIESEL)
    client.post(f"/api/claims/{a}/approve")  # a -> approved
    b = _upload(client, fake_ocr, Extraction(
        expense_type="electricity", quantity=Decimal("12000"), unit="kWh"))  # in_review

    owner = Session(bind=db_engine, future=True, expire_on_commit=False)
    firm_b = None
    try:
        firm_b = Firm(name="Web RLS B")
        owner.add(firm_b)
        owner.flush()
        client_b = Client(firm_id=firm_b.id, name="B Co", currency="MYR")
        owner.add(client_b)
        owner.flush()
        other = Claim(
            firm_id=firm_b.id, client_id=client_b.id,
            image_path="/o.png", image_sha256="o", status="in_review",
        )
        owner.add(other)
        owner.flush()
        owner.commit()
        other_id = str(other.id)

        page = client.get("/claims")
        assert page.status_code == 200
        assert a in page.text and b in page.text       # own claims listed
        assert other_id not in page.text               # another firm RLS-excluded

        approved = client.get("/claims?status=approved")
        assert a in approved.text and b not in approved.text   # status filter
    finally:
        if firm_b is not None:
            owner.execute(text("DELETE FROM claim WHERE firm_id = :f"), {"f": firm_b.id})
            owner.execute(text("DELETE FROM client WHERE firm_id = :f"), {"f": firm_b.id})
            owner.execute(text("DELETE FROM firm WHERE id = :f"), {"f": firm_b.id})
            owner.commit()
        owner.close()


# 2 -------------------------------------------------------------------------
def test_review_page_renders_with_actions(client, fake_ocr):
    cid = _upload(client, fake_ocr, _DIESEL)
    page = client.get(f"/claims/{cid}/review")
    assert page.status_code == 200
    # The partner can review → the three lifecycle actions are drawn and wired.
    assert "Approve" in page.text
    assert f"/claims/{cid}/approve" in page.text
    assert f"/claims/{cid}/send-back" in page.text
    assert f"/claims/{cid}/reject" in page.text
    assert f'src="/claims/{cid}/image"' in page.text   # receipt image embedded


# 3 -------------------------------------------------------------------------
def test_review_page_flags_unmapped(client, fake_ocr):
    cid = _upload(client, fake_ocr, Extraction(expense_type="other", total_amount=Decimal("100")))
    page = client.get(f"/claims/{cid}/review")
    assert page.status_code == 200
    assert "Unmapped" in page.text   # data_quality flag rendered


# 4 -------------------------------------------------------------------------
def test_web_approve_transitions(client, fake_ocr):
    cid = _upload(client, fake_ocr, _DIESEL)
    r = client.post(f"/claims/{cid}/approve", follow_redirects=False)
    assert r.status_code == 303
    assert _status(client, cid) == "approved"


# 5 -------------------------------------------------------------------------
def test_web_send_back_transitions(client, fake_ocr):
    cid = _upload(client, fake_ocr, _DIESEL)
    r = client.post(f"/claims/{cid}/send-back", data={"reason": "missing GST"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _status(client, cid) == "submitted"
    events = client.get(f"/api/audit/{cid}").json()
    sent_back = next(e for e in events if e["event_type"] == "sent_back")
    assert sent_back["detail"]["reason"] == "missing GST"


# 6 -------------------------------------------------------------------------
def test_web_reject_transitions(client, fake_ocr):
    cid = _upload(client, fake_ocr, _DIESEL)
    r = client.post(f"/claims/{cid}/reject", data={"reason": "duplicate"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _status(client, cid) == "rejected"


# 7 -------------------------------------------------------------------------
def test_web_assign_category_clears_unmapped(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="other", quantity=Decimal("450"), unit="L", total_amount=Decimal("2000")))
    assert client.get(f"/api/claims/{cid}").json()["data_quality"].startswith("Unmapped")

    diesel = db_session.execute(
        select(Category).filter_by(
            client_id=db_session.info["principal"]["client"], expense_type="fuel_diesel")
    ).scalar_one()
    r = client.post(f"/claims/{cid}/category", data={"category_id": str(diesel.id)},
                    follow_redirects=False)
    assert r.status_code == 303

    after = client.get(f"/api/claims/{cid}").json()
    assert after["category_id"] == str(diesel.id)
    assert after["scope"] == 1 and after["data_quality"] == "Activity-based"
