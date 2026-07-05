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
    assert "no category" in page.text   # unmapped line flagged for review


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
    assert client.get(f"/api/claims/{cid}").json()["category_id"] is None

    diesel = db_session.execute(
        select(Category).filter_by(
            client_id=db_session.info["principal"]["client"], expense_type="fuel_diesel")
    ).scalar_one()
    r = client.post(f"/claims/{cid}/category", data={"category_id": str(diesel.id)},
                    follow_redirects=False)
    assert r.status_code == 303

    after = client.get(f"/api/claims/{cid}").json()
    assert after["category_id"] == str(diesel.id)
    assert after["carbon_relevant"] is True


def test_review_shows_gl_inherited_from_category(client, fake_ocr, db_session):
    """A line coded purely by its category (no own gl_code override) must still
    SHOW the GL on review — the category's gl_export_code is inherited into the
    field, not left blank. This is the fix for 'GL not showing when a category
    with a GL is already chosen'."""
    diesel = db_session.execute(
        select(Category).filter_by(
            client_id=db_session.info["principal"]["client"], expense_type="fuel_diesel")
    ).scalar_one()
    diesel.gl_export_code = "6410"
    db_session.commit()

    cid = _upload(client, fake_ocr, _DIESEL)   # auto-maps to the diesel category
    assert client.get(f"/api/claims/{cid}").json()["category_id"] == str(diesel.id)

    page = client.get(f"/claims/{cid}/review")
    assert page.status_code == 200
    assert 'value="6410"' in page.text        # inherited GL is shown in the field
    assert "From category" in page.text        # and labelled as inherited


def test_receipt_image_endpoint_downloads_with_filename(client, fake_ocr):
    """The download button must SAVE the receipt, not just open it — the endpoint
    sends Content-Disposition: attachment with a friendly filename."""
    cid = _upload(client, fake_ocr, _DIESEL)
    r = client.get(f"/claims/{cid}/image")
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd and "receipt-" in cd


# 8b ------------------------------------------------------------------------
def test_approved_claim_is_locked_until_unapproved(client, fake_ocr):
    """An approved claim cannot be amended directly; you unapprove it (back to
    in_review) first, then amend. Released/exported data would stay locked."""
    cid = _upload(client, fake_ocr, _DIESEL)
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    assert _status(client, cid) == "approved"

    # Direct edit of an approved claim is refused — status unchanged.
    client.post(f"/claims/{cid}/edit", data={"line_id": "", "vendor": "Nope"},
                follow_redirects=False)
    assert _status(client, cid) == "approved"
    assert client.get(f"/api/claims/{cid}").json()["vendor"] != "Nope"

    # Unapprove reopens it to in_review.
    u = client.post(f"/claims/{cid}/unapprove", follow_redirects=False)
    assert u.status_code == 303
    assert _status(client, cid) == "in_review"

    # Now it is editable again.
    client.post(f"/claims/{cid}/edit", data={"line_id": "", "vendor": "Shell-2"},
                follow_redirects=False)
    assert client.get(f"/api/claims/{cid}").json()["vendor"] == "Shell-2"

    events = [e["event_type"] for e in client.get(f"/api/audit/{cid}").json()]
    assert events == ["submitted", "approved", "unapproved", "edited"]


# 7b ------------------------------------------------------------------------
def test_inbox_search_and_export(client, fake_ocr):
    """The inbox search (?q=) filters by vendor/title and the Export control is a
    real CSV link — not dead chrome."""
    a = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", vendor="Shell", quantity=Decimal("1"), unit="L"))
    b = _upload(client, fake_ocr, Extraction(
        expense_type="electricity", vendor="TNB", quantity=Decimal("1"), unit="kWh"))

    page = client.get("/claims?q=Shell")
    assert a in page.text and b not in page.text          # vendor search filters
    assert 'href="/api/claims/export' in page.text         # working CSV export link


# 8 -------------------------------------------------------------------------
def test_nav_shell_renders_live_counts_and_scope(client, fake_ocr, db_session):
    """The sidebar badges + topbar scope are driven by real per-status counts and
    the tenant's client name, not the mockup's hardcoded 42/23/8 placeholders."""
    from eclaim.db.models import Client

    a = _upload(client, fake_ocr, _DIESEL)
    client.post(f"/api/claims/{a}/approve")            # -> approved
    _upload(client, fake_ocr, Extraction(
        expense_type="electricity", quantity=Decimal("12000"), unit="kWh"))  # in_review

    name = db_session.get(Client, db_session.info["principal"]["client"]).name
    page = client.get("/claims").text

    # Rail shows the real tenant client (entity footer).
    assert name in page
    # Live badges: 2 total, 1 awaiting review, 1 to approve — and none of the
    # mockup's static counts survive.
    assert '<span class="oc-cnt">2</span>' in page             # All claims total
    assert '<span class="oc-cnt warn">1</span>' in page        # Awaiting review
    assert '<span class="oc-cnt alert">1</span>' in page       # To approve
    assert ">42<" not in page and ">23<" not in page
