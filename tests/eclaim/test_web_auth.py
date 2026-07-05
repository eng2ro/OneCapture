"""Browser session login for the web UI (cookie carrying the signed token).

Uses the ``browser`` fixture — same db/ocr overrides as ``client`` but NO
principal override — so the real cookie-session path runs end to end. The seeded
firm user is ``partner@seed.test`` (conftest ``_seed``).
"""

from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from eclaim.config import get_settings

SEED_EMAIL = "partner@seed.test"


def _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, base_role: str):
    """A TestClient whose web/API principal is forced to ``base_role`` — so the
    Manage role gate can be exercised for a non-firm-scoped user (viewer/approver)
    without minting real grants. Mirrors the ``client`` fixture's overrides."""
    from eclaim.api import deps
    from eclaim.api.app import create_app
    from eclaim.auth.principal import Principal

    def _override_db():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    def _principal(request: Request) -> Principal:
        ids = db_session.info["principal"]
        principal = Principal(
            user_id=ids["user"], firm_id=ids["firm"], base_role=base_role,
            allowed_client_ids=frozenset({ids["client"]}), email=f"{base_role}@seed.test",
        )
        request.state.principal = principal
        request.state.db = db_session
        return principal

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    app.dependency_overrides[deps.get_principal] = _principal
    app.dependency_overrides[deps.get_session_principal] = _principal
    app.dependency_overrides[deps.get_ocr] = lambda: fake_ocr
    app.dependency_overrides[deps.get_segmenter] = lambda: fake_segmenter
    app.dependency_overrides[deps.get_image_dir] = lambda: tmp_path
    return app


def test_login_page_renders(browser):
    page = browser.get("/login")
    assert page.status_code == 200
    assert "Sign in" in page.text
    assert 'action="/login"' in page.text


def test_bad_credentials_rerender_with_error_and_no_cookie(browser):
    resp = browser.post("/login", data={"email": "nobody@nowhere.test"}, follow_redirects=False)
    assert resp.status_code == 200            # re-rendered form, not a redirect
    assert "unknown user" in resp.text
    assert "oc_session" not in resp.headers.get("set-cookie", "")   # no session issued


def test_good_credentials_set_cookie_and_redirect(browser):
    resp = browser.post("/login", data={"email": SEED_EMAIL}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/claims"
    set_cookie = resp.headers.get("set-cookie", "")
    assert "oc_session=" in set_cookie
    # HttpOnly always; Secure tracks the deployment setting (off for local http
    # dev, forced on in production by config.assert_production_safe).
    assert "HttpOnly" in set_cookie
    assert ("Secure" in set_cookie) == get_settings().session_cookie_secure


def test_authenticated_cookie_reaches_inbox(browser):
    login = browser.post("/login", data={"email": SEED_EMAIL}, follow_redirects=False)
    token = login.cookies.get("oc_session")
    assert token

    browser.cookies.set("oc_session", token)   # the browser now carries the session
    page = browser.get("/claims", follow_redirects=False)
    assert page.status_code == 200
    assert "Claims" in page.text


def test_no_cookie_redirects_to_login(browser):
    resp = browser.get("/claims", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_logout_clears_cookie(browser):
    resp = browser.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    set_cookie = resp.headers.get("set-cookie", "")
    assert "oc_session=" in set_cookie and "Max-Age=0" in set_cookie


# --------------------------------------------------------------------------- #
# Manage role gate: the admin master pages are firm-scope only. A viewer sees no
# Manage links and, if they hit the URL directly, gets a friendly page not JSON.
# --------------------------------------------------------------------------- #
def test_firm_scoped_partner_sees_manage_links(db_session, fake_ocr, fake_segmenter, tmp_path):
    app = _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, "partner")
    with TestClient(app) as c:
        page = c.get("/claims")
    assert page.status_code == 200
    assert "/admin/categories" in page.text  # firm-scope link is present


def test_viewer_manage_links_hidden(db_session, fake_ocr, fake_segmenter, tmp_path):
    app = _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, "viewer")
    with TestClient(app) as c:
        page = c.get("/claims")
    assert page.status_code == 200
    # No admin master links for a non-firm-scoped role...
    assert "/admin/categories" not in page.text
    assert "/admin/events" not in page.text
    # ...but the read-only handoff view stays available to everyone.
    assert "/ledger" in page.text


@pytest.mark.parametrize("path", ["/admin/categories", "/admin/claimants", "/admin/events"])
def test_viewer_admin_page_shows_friendly_forbidden(
    db_session, fake_ocr, fake_segmenter, tmp_path, path
):
    app = _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, "viewer")
    with TestClient(app) as c:
        resp = c.get(path)
    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("text/html")  # a page, not JSON
    assert "This area is for firm administrators" in resp.text


@pytest.mark.parametrize("path", ["/admin/categories", "/admin/claimants", "/admin/events"])
def test_blank_edit_param_shows_list_not_422(
    db_session, fake_ocr, fake_segmenter, tmp_path, path
):
    # A malformed/empty ?edit= must fall back to the list, never a 422.
    app = _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, "partner")
    with TestClient(app) as c:
        assert c.get(f"{path}?edit=").status_code == 200
        assert c.get(f"{path}?edit=not-a-uuid").status_code == 200
