"""CSRF protection on the cookie-authenticated web surface (blocker B6).

Driven through the real cookie-session path (the ``browser`` fixture: no principal
override, so login mints the ``oc_session`` cookie and the guard sees it). Proves a
state-changing POST is rejected without the session-bound token and accepted with
it, via both transports (form field and header) — and that the guard does NOT
over-block unauthenticated or safe requests.
"""

from __future__ import annotations

import re

SEED_EMAIL = "partner@seed.test"


def _login(browser) -> None:
    resp = browser.post("/login", data={"email": SEED_EMAIL}, follow_redirects=False)
    assert resp.status_code == 303
    # httpx TestClient stores the Set-Cookie, so later requests carry oc_session.


def _page_token(browser) -> str:
    """The CSRF token the server rendered into the page (meta tag)."""
    page = browser.get("/claims")
    assert page.status_code == 200
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert m and m.group(1), "no csrf token rendered for the authenticated page"
    return m.group(1)


def test_cookie_post_without_token_is_forbidden(browser):
    _login(browser)
    resp = browser.post("/logout", follow_redirects=False)
    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("text/html")  # friendly page, not JSON


def test_cookie_post_with_header_token_succeeds(browser):
    _login(browser)
    token = _page_token(browser)
    resp = browser.post(
        "/logout", headers={"X-CSRF-Token": token}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_cookie_post_with_form_field_token_succeeds(browser):
    _login(browser)
    token = _page_token(browser)
    resp = browser.post("/logout", data={"_csrf": token}, follow_redirects=False)
    assert resp.status_code == 303


def test_cookie_post_with_wrong_token_is_forbidden(browser):
    _login(browser)
    _page_token(browser)  # a valid token exists, but we send a forged one
    resp = browser.post(
        "/logout", headers={"X-CSRF-Token": "not-the-real-token"}, follow_redirects=False
    )
    assert resp.status_code == 403


def test_token_is_session_bound_not_reusable():
    """A token minted for one session must not validate against a different session
    cookie — the whole point of binding it to the cookie value (a naive
    double-submit would accept any attacker-chosen pair). Unit-level so it's exact
    and never timing-dependent."""
    from eclaim.auth import csrf

    secret = "unit-test-secret"
    a = csrf.issue("session-A", secret=secret)
    b = csrf.issue("session-B", secret=secret)
    assert a != b
    assert csrf.valid("session-A", a, secret=secret)
    assert not csrf.valid("session-A", b, secret=secret)  # B's token rejected on A
    assert not csrf.valid("session-A", None, secret=secret)
    assert not csrf.valid("session-A", "", secret=secret)


def test_unauthenticated_post_is_not_csrf_blocked(browser):
    """No session cookie → nothing to forge against. The guard must stand aside and
    let the route's own auth handle it (logout just clears + redirects), so we never
    turn an anonymous request into a confusing 403."""
    resp = browser.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.status_code != 403


def test_safe_get_is_never_blocked(browser):
    _login(browser)
    assert browser.get("/claims").status_code == 200
