"""Cookie-authed server-rendered capture (GET/POST /capture).

A hand-keyed web entry point into the SAME ClaimService.upload the bearer API
uses — the form supplies the Extraction fields, a manual provider feeds them in,
and classification runs through the category path as usual. The JSON
/api/claims/upload stays bearer-only and untouched.
"""

from __future__ import annotations

import json
import re
import uuid

from decimal import Decimal

from sqlalchemy import func, select

from eclaim.db.models import Category, Claim, ClaimLine, Event
from eclaim.ocr.base import Extraction, OcrError


def _lines(db_session, claim_id):
    return db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == claim_id).order_by(ClaimLine.line_no)
    ).scalars().all()


def _files(n=1):
    return [("files", (f"r{i}.png", b"\x89PNG\r\n fake", "image/png")) for i in range(n)]


def _capture(client, items=None, n=None):
    """POST a batch of receipts + their per-file verified fields to /capture.
    ``items`` aligns to the files by order; ``n`` defaults to len(items) or 1."""
    items = items or []
    if n is None:
        n = len(items) or 1
    return client.post(
        "/capture", files=_files(n), data={"items": json.dumps(items), "attested": "yes"},
        follow_redirects=False,
    )


def _claim_id(resp) -> str:
    m = re.match(r"^/claims/([0-9a-f-]+)/review$", resp.headers["location"])
    assert m, resp.headers.get("location")
    return m.group(1)


def _category(db_session, expense_type) -> Category:
    return db_session.execute(
        select(Category).where(Category.expense_type == expense_type)
    ).scalars().first()


def test_capture_page_renders_for_logged_in_user(client):
    page = client.get("/capture")
    assert page.status_code == 200
    assert "Submit expense claims" in page.text
    assert 'action="/capture"' in page.text
    # Batch uploader: a multi-file input + the auto-capture extract endpoint.
    assert 'name="files"' in page.text and "multiple" in page.text
    assert "/capture/extract" in page.text


def test_capture_page_is_not_cached(client):
    """The capture page's inline JS carries the classifier verdict in the POST payload;
    a stale cached copy would drop it and mis-file vendor bills as expenses. It must be
    served no-store so the browser can't run an old version."""
    page = client.get("/capture")
    assert "no-store" in page.headers.get("cache-control", "").lower()


def test_post_capture_creates_claim_via_category_path_and_redirects(client, db_session):
    resp = _capture(client, items=[{
        "expense_type": "fuel_diesel", "quantity": "450", "unit": "L",
        "total_amount": "2000", "vendor": "Shell", "doc_no": "INV-3",
    }])
    assert resp.status_code == 303
    cid = _claim_id(resp)   # a single receipt lands straight on its review page

    # RLS-scoped to the principal's client (client_id isn't in ClaimOut → read the row).
    assert db_session.get(Claim, uuid.UUID(cid)).client_id == db_session.info["principal"]["client"]

    claim = client.get(f"/api/claims/{cid}").json()
    assert claim["status"] == "in_review"
    assert claim["vendor"] == "Shell"   # flattened from the claim's single line
    # Mapped through the category path, exactly as an OCR upload would be.
    assert claim["category_id"] is not None
    assert claim["carbon_relevant"] is True
    assert len(claim["lines"]) == 1


def test_post_capture_unmapped_expense_is_flagged(client):
    resp = _capture(client, items=[{"expense_type": "other", "total_amount": "100"}])
    assert resp.status_code == 303
    cid = _claim_id(resp)
    claim = client.get(f"/api/claims/{cid}").json()
    assert claim["category_id"] is None
    assert claim["carbon_relevant"] is True   # unmapped defaults relevant (not dropped)


def test_no_cookie_capture_redirects_to_login(browser):
    assert browser.get("/capture", follow_redirects=False).status_code == 303
    assert browser.get("/capture", follow_redirects=False).headers["location"] == "/login"


def test_api_upload_still_requires_bearer(browser):
    # The cookie-less browser hitting the JSON API gets 401 — bearer unchanged.
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    resp = browser.post("/api/claims/upload", files=files)
    assert resp.status_code == 401


# --- auto-capture: /capture/extract reads the receipt, creates no claim ------
def test_capture_extract_returns_fields_and_suggested_category(client, fake_ocr, db_session):
    fake_ocr.extraction = Extraction(
        vendor="Shell", doc_no="INV-9", date="2025-09-26",
        total_amount=Decimal("70.00"), expense_type="fuel_diesel",
        quantity=Decimal("34.146"), unit="L", confidence=Decimal("0.92"),
    )
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    body = client.post("/capture/extract", files=files).json()

    assert body["ok"] is True
    assert body["extraction"]["vendor"] == "Shell"
    assert body["extraction"]["total_amount"] == "70.00"
    assert body["extraction"]["quantity"] == "34.146"
    # fuel_diesel maps to exactly one category → suggested for pre-selection.
    assert body["suggested_category_id"] == str(_category(db_session, "fuel_diesel").id)
    # No claim was created by reading alone.
    assert db_session.execute(select(Claim)).scalars().first() is None


def test_capture_extract_unsupported_media_is_rejected(client):
    files = {"file": ("r.pdf", b"%PDF-1.4", "application/pdf")}
    resp = client.post("/capture/extract", files=files)
    assert resp.status_code == 415
    assert resp.json()["ok"] is False


def test_capture_extract_degrades_when_ocr_fails(client, fake_ocr):
    def _boom(*_a, **_k):
        raise OcrError("no key configured")
    fake_ocr.extract = _boom
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    body = client.post("/capture/extract", files=files).json()
    assert body["ok"] is False and body["reason"]   # friendly fallback, not a 500


# --- category-first submit: staff picks the category explicitly --------------
def test_capture_uses_explicitly_chosen_category(client, db_session):
    elec = _category(db_session, "electricity")
    resp = _capture(client, items=[{
        "category_id": str(elec.id), "expense_type": "electricity",
        "quantity": "1200", "unit": "kWh", "total_amount": "900", "vendor": "TNB",
    }])
    assert resp.status_code == 303
    claim = client.get(f"/api/claims/{_claim_id(resp)}").json()
    assert claim["category_id"] == str(elec.id)
    assert claim["carbon_relevant"] is True


# --- batch: many receipts in one submit → ONE claim with many lines ----------
def test_batch_upload_creates_one_claim_with_many_lines(client, db_session):
    elec = _category(db_session, "electricity")
    resp = _capture(client, items=[
        {"expense_type": "fuel_diesel", "quantity": "100", "unit": "L",
         "total_amount": "500", "vendor": "Shell"},
        {"expense_type": "other", "total_amount": "42", "vendor": "Starbucks"},
        {"category_id": str(elec.id), "expense_type": "electricity",
         "quantity": "50", "unit": "kWh", "total_amount": "30", "vendor": "TNB"},
    ])
    assert resp.status_code == 303
    cid = _claim_id(resp)   # a single claim — its review screen
    claims = db_session.execute(select(Claim)).scalars().all()
    assert len(claims) == 1 and claims[0].status == "in_review"
    lines = _lines(db_session, claims[0].id)
    assert len(lines) == 3
    assert {ln.vendor for ln in lines} == {"Shell", "Starbucks", "TNB"}
    # Header total rolls up the three line amounts.
    assert claims[0].total_claimed == Decimal("572.00")


def test_batch_without_items_falls_back_to_server_ocr(client, fake_ocr, db_session):
    fake_ocr.extraction = Extraction(
        expense_type="fuel_diesel", quantity=Decimal("10"), unit="L",
        total_amount=Decimal("50"),
    )
    resp = client.post("/capture", files=_files(2), data={"items": "[]", "attested": "yes"},
                       follow_redirects=False)
    assert resp.status_code == 303
    # No client-side extraction → each file is OCR'd server-side: one claim, 2 lines.
    assert db_session.execute(select(func.count()).select_from(Claim)).scalar_one() == 1
    claim = db_session.execute(select(Claim)).scalars().one()
    assert len(_lines(db_session, claim.id)) == 2


def test_batch_unread_receipts_are_ocrd_not_saved_blank(client, fake_ocr, db_session):
    """Regression: a receipt the page hadn't finished reading (null or all-null
    item) must be OCR'd server-side, not saved as an empty 'other' line."""
    fake_ocr.extraction = Extraction(
        vendor="Server-OCR", expense_type="other", total_amount=Decimal("9"),
    )
    items = [
        {"expense_type": "fuel_diesel", "quantity": "100", "unit": "L",
         "total_amount": "500", "vendor": "Shell"},          # read client-side
        None,                                                 # not read yet
        {"expense_type": "other", "vendor": None, "total_amount": None},  # all-null
    ]
    resp = client.post("/capture", files=_files(3), data={"items": json.dumps(items), "attested": "yes"},
                       follow_redirects=False)
    assert resp.status_code == 303
    claim = db_session.execute(select(Claim)).scalars().one()
    lines = _lines(db_session, claim.id)
    assert len(lines) == 3
    # The Shell line kept its client data; the two unread ones were read server-side
    # (vendor 'Server-OCR') — none came through blank.
    assert sorted(ln.vendor for ln in lines) == ["Server-OCR", "Server-OCR", "Shell"]


def test_review_offers_verify_next_across_separate_claims(client, db_session):
    # Two separate submissions → two in_review claims → the verify-next queue.
    _capture(client, items=[{"expense_type": "other", "total_amount": "10", "vendor": "A"}])
    _capture(client, items=[{"expense_type": "other", "total_amount": "20", "vendor": "B"}])
    ids = [
        c.id for c in db_session.execute(
            select(Claim).where(Claim.status == "in_review").order_by(Claim.created_at.desc())
        ).scalars()
    ]
    assert len(ids) == 2
    page = client.get(f"/claims/{ids[0]}/review").text
    assert "Verify next" in page and "1 more to verify" in page
    # links to the OTHER in_review claim, not itself
    assert f"/claims/{ids[1]}/review" in page


# --- merchant -> category auto-assign (the McDonald's fix) -------------------
def _add_category(db_session, name, expense_type, carbon_relevant=True):
    ids = db_session.info["principal"]
    cat = Category(
        firm_id=ids["firm"], client_id=ids["client"], name=name,
        expense_type=expense_type, carbon_relevant=carbon_relevant,
    )
    db_session.add(cat)
    db_session.flush()
    return cat


def test_extract_suggests_meals_for_mcdonalds(client, fake_ocr, db_session):
    """McDonald's reads as expense_type 'other' (no carbon type), but the merchant
    name resolves to the Meals category — the bug the user hit, now fixed."""
    meals = _add_category(db_session, "Meals", "meals")
    fake_ocr.extraction = Extraction(
        vendor="McDonald's Bourke & Russell St", expense_type="other",
        total_amount=Decimal("6.85"),
    )
    body = client.post(
        "/capture/extract",
        files={"file": ("r.png", b"\x89PNG\r\n fake", "image/png")},
    ).json()
    assert body["ok"] is True
    assert body["suggested_category_id"] == str(meals.id)


def test_extract_ocr_specific_type_wins_over_merchant(client, fake_ocr, db_session):
    """A Shell receipt the OCR typed as fuel_diesel keeps Diesel (OCR's specific
    type) rather than being pulled to Petrol by the merchant rule."""
    diesel = _category(db_session, "fuel_diesel")
    fake_ocr.extraction = Extraction(
        vendor="Shell Select", expense_type="fuel_diesel",
        quantity=Decimal("40"), unit="L", total_amount=Decimal("180"),
    )
    body = client.post(
        "/capture/extract",
        files={"file": ("r.png", b"\x89PNG\r\n fake", "image/png")},
    ).json()
    assert body["suggested_category_id"] == str(diesel.id)


def test_server_ocr_line_auto_categorised_by_merchant(client, fake_ocr, db_session):
    """The server-OCR path (no client-side item) also merchant-maps: a McDonald's
    receipt lands on Meals, not unmapped."""
    meals = _add_category(db_session, "Meals", "meals")
    fake_ocr.extraction = Extraction(
        vendor="McDonald's KLCC", expense_type="other", total_amount=Decimal("12"),
    )
    resp = client.post("/capture", files=_files(1), data={"items": "[]", "attested": "yes"},
                       follow_redirects=False)
    assert resp.status_code == 303
    claim = db_session.execute(select(Claim)).scalars().one()
    line = _lines(db_session, claim.id)[0]
    assert line.category_id == meals.id
    assert line.carbon_relevant is True


# --- claim-level type + conditional date range (migration 0010) --------------
def _post_capture(client, *, items=None, n=None, **data):
    """POST /capture with arbitrary header fields (claim_type, dates, event_id, …)
    on top of a one-line receipt batch."""
    items = items or [{"expense_type": "other", "total_amount": "10", "vendor": "A"}]
    if n is None:
        n = len(items) or 1
    payload = {"items": json.dumps(items), "attested": "yes"}
    payload.update(data)
    return client.post("/capture", files=_files(n), data=payload, follow_redirects=False)


def test_capture_page_shows_claim_type_and_new_trip(client):
    page = client.get("/capture").text
    assert 'name="claim_type"' in page
    # The inline "+ New trip" affordance + its sentinel value.
    assert "__new__" in page and "New trip" in page


def test_general_claim_needs_no_dates(client, db_session):
    # Default type is 'general' — submitting without any date still works.
    resp = _post_capture(client)
    assert resp.status_code == 303
    claim = db_session.get(Claim, uuid.UUID(_claim_id(resp)))
    assert claim.claim_type == "general"
    assert claim.start_date is None and claim.end_date is None


def test_non_general_standalone_claim_requires_dates(client, db_session):
    # A training claim with no event and no dates is rejected (re-renders the form).
    resp = _post_capture(client, claim_type="training")
    assert resp.status_code == 200
    assert "needs a start and end date" in resp.text
    # Nothing was persisted.
    assert db_session.execute(select(func.count()).select_from(Claim)).scalar_one() == 0


def test_non_general_claim_with_dates_succeeds(client, db_session):
    resp = _post_capture(
        client, claim_type="travel", start_date="2026-03-12", end_date="2026-03-14"
    )
    assert resp.status_code == 303
    claim = db_session.get(Claim, uuid.UUID(_claim_id(resp)))
    assert claim.claim_type == "travel"
    assert str(claim.start_date) == "2026-03-12" and str(claim.end_date) == "2026-03-14"


def test_claim_end_date_before_start_is_rejected(client, db_session):
    resp = _post_capture(
        client, claim_type="travel", start_date="2026-03-14", end_date="2026-03-12"
    )
    assert resp.status_code == 200
    assert "end date is before the start date" in resp.text
    assert db_session.execute(select(func.count()).select_from(Claim)).scalar_one() == 0


def test_inline_new_trip_creates_event_and_links_claim(client, db_session):
    resp = _post_capture(
        client, event_id="__new__", new_event_title="KL — Sales Training",
        new_event_start="2026-03-12", new_event_end="2026-03-14",
    )
    assert resp.status_code == 303
    claim = db_session.get(Claim, uuid.UUID(_claim_id(resp)))
    assert claim.event_id is not None
    ev = db_session.get(Event, claim.event_id)
    assert ev.title == "KL — Sales Training"
    assert str(ev.start_date) == "2026-03-12" and str(ev.end_date) == "2026-03-14"
    # Budget is left for the manager; a brand-new trip isn't a 'general' claim.
    assert ev.budget_amount is None
    assert claim.claim_type == "travel"


def test_inline_new_trip_without_dates_is_rejected(client, db_session):
    resp = _post_capture(client, event_id="__new__", new_event_title="No dates")
    assert resp.status_code == 200
    assert "new trip needs a start and end date" in resp.text
    assert db_session.execute(select(func.count()).select_from(Event)).scalar_one() == 0


def test_existing_event_claim_needs_no_claim_dates(client, db_session):
    # Attaching an existing event supplies the dates → a non-general claim with no
    # claim-level dates is still accepted.
    ids = db_session.info["principal"]
    ev = Event(firm_id=ids["firm"], client_id=ids["client"], title="Q1 Roadshow",
               event_type="travel")
    db_session.add(ev)
    db_session.flush()
    resp = _post_capture(client, claim_type="training", event_id=str(ev.id))
    assert resp.status_code == 303
    claim = db_session.get(Claim, uuid.UUID(_claim_id(resp)))
    assert claim.event_id == ev.id and claim.claim_type == "training"


def test_capture_persists_document_header_fields(client, db_session):
    """The capture form carries the grouping header — purpose, remarks and the
    document posting date — onto the claim header (not the lines)."""
    resp = _post_capture(
        client, purpose="Q1 client visit", remarks="paid by personal card",
        posting_date="2026-06-29",
    )
    assert resp.status_code == 303
    claim = db_session.get(Claim, uuid.UUID(_claim_id(resp)))
    assert claim.purpose == "Q1 client visit"
    assert claim.remarks == "paid by personal card"
    assert str(claim.posting_date) == "2026-06-29"


def test_capture_page_shows_header_fields(client):
    # Purpose + remarks live under the optional "more details" disclosure; the
    # accounting posting date is NOT a capture field (finance sets it at review),
    # so the capture form must stay simple.
    page = client.get("/capture").text
    assert 'name="purpose"' in page
    assert 'name="remarks"' in page
    assert 'name="posting_date"' not in page


def test_many_spend_categories_share_expense_type_other(db_session):
    """0007: a client can now have several spend-based categories all on
    expense_type='other' — the old UNIQUE(client_id, expense_type) is gone."""
    ids = db_session.info["principal"]
    for name in ("Meals", "Taxi", "Parking"):
        db_session.add(Category(
            firm_id=ids["firm"], client_id=ids["client"],
            name=name, expense_type="other", factor_key=None,
        ))
    db_session.flush()   # would raise IntegrityError under the old constraint
    others = db_session.execute(
        select(Category).where(Category.expense_type == "other")
    ).scalars().all()
    assert {c.name for c in others} >= {"Meals", "Taxi", "Parking"}
