"""Cookie-authed server-rendered capture (GET/POST /capture).

A hand-keyed web entry point into the SAME ClaimService.upload the bearer API
uses — the form supplies the Extraction fields, a manual provider feeds them in,
and classification runs through the category path as usual. The JSON
/api/claims/upload stays bearer-only and untouched.
"""

from __future__ import annotations

import re
import uuid

from eclaim.db.models import Claim


def _capture(client, **fields):
    """POST a receipt + fields to /capture; return the response (no redirect follow)."""
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    return client.post("/capture", files=files, data=fields, follow_redirects=False)


def _claim_id(resp) -> str:
    m = re.match(r"^/claims/([0-9a-f-]+)/review$", resp.headers["location"])
    assert m, resp.headers.get("location")
    return m.group(1)


def test_capture_page_renders_for_logged_in_user(client):
    page = client.get("/capture")
    assert page.status_code == 200
    assert "Capture a receipt" in page.text
    assert 'action="/capture"' in page.text
    assert "fuel_diesel" in page.text   # the expense_type dropdown is populated


def test_post_capture_creates_claim_via_category_path_and_redirects(client, db_session):
    resp = _capture(client, expense_type="fuel_diesel", quantity="450", unit="L",
                    total_amount="2000", vendor="Shell", doc_no="INV-3")
    assert resp.status_code == 303
    cid = _claim_id(resp)

    # RLS-scoped to the principal's client (client_id isn't in ClaimOut → read the row).
    assert db_session.get(Claim, uuid.UUID(cid)).client_id == db_session.info["principal"]["client"]

    claim = client.get(f"/api/claims/{cid}").json()
    assert claim["status"] == "in_review"
    assert claim["vendor"] == "Shell"
    # Classified through the category path, exactly as an OCR upload would be.
    assert claim["scope"] == 1 and claim["factor_key"] == "fuel_diesel"
    assert claim["category_id"] is not None
    assert claim["tco2e"] == "1.206000" and claim["data_quality"] == "Activity-based"


def test_post_capture_unmapped_expense_is_flagged(client):
    resp = _capture(client, expense_type="other", total_amount="100")
    assert resp.status_code == 303
    cid = _claim_id(resp)
    claim = client.get(f"/api/claims/{cid}").json()
    assert claim["category_id"] is None
    assert claim["data_quality"].startswith("Unmapped")


def test_no_cookie_capture_redirects_to_login(browser):
    assert browser.get("/capture", follow_redirects=False).status_code == 303
    assert browser.get("/capture", follow_redirects=False).headers["location"] == "/login"


def test_api_upload_still_requires_bearer(browser):
    # The cookie-less browser hitting the JSON API gets 401 — bearer unchanged.
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    resp = browser.post("/api/claims/upload", files=files)
    assert resp.status_code == 401
