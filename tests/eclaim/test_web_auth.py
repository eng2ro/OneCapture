"""Browser session login for the web UI (cookie carrying the signed token).

Uses the ``browser`` fixture — same db/ocr overrides as ``client`` but NO
principal override — so the real cookie-session path runs end to end. The seeded
firm user is ``partner@seed.test`` (conftest ``_seed``).
"""

from __future__ import annotations

SEED_EMAIL = "partner@seed.test"


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
    assert "HttpOnly" in set_cookie and "Secure" in set_cookie   # the cookie is hardened


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
