"""ERP Sync review web surface is cookie-authenticated + CSRF-protected (P7).

The review queue/entry pages are a BROWSER surface: a logged-in reviewer carries the
oc_session cookie, not a bearer token. Previously the pages used the bearer
``get_principal`` and the buttons posted to the bearer JSON API, so a real browser
session got 401. These tests drive the real cookie path (the ``browser`` fixture:
login mints the cookie, no principal override) to prove:

* the queue page loads under a cookie session (not a bearer 401);
* the action routes are cookie-authenticated (a token-bearing post succeeds — not 401);
* the action routes are CSRF-protected (a cookie post with no session-bound token 403s).
"""

from __future__ import annotations

import re
import uuid

SEED_EMAIL = "partner@seed.test"


def _login(browser) -> None:
    assert browser.post(
        "/login", data={"email": SEED_EMAIL}, follow_redirects=False
    ).status_code == 303


def _token(browser) -> str:
    page = browser.get("/erpsync/review")
    assert page.status_code == 200
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert m and m.group(1), "no csrf token rendered on the erpsync page"
    return m.group(1)


def test_queue_page_loads_under_cookie_session(browser):
    """The page authenticates by cookie now — a bearer-only page would 401 here."""
    _login(browser)
    assert browser.get("/erpsync/review").status_code == 200


def test_release_requires_csrf_and_is_cookie_authenticated(browser, db_session):
    cid = db_session.info["principal"]["client"]
    _login(browser)

    # No CSRF token → rejected even with a valid cookie session (friendly 403 page).
    blocked = browser.post(
        f"/erpsync/review/clients/{cid}/release", follow_redirects=False
    )
    assert blocked.status_code == 403

    # With the session-bound token → accepted (cookie-authenticated, not a bearer 401).
    ok = browser.post(
        f"/erpsync/review/clients/{cid}/release",
        headers={"X-CSRF-Token": _token(browser)}, follow_redirects=False,
    )
    assert ok.status_code == 200 and ok.json() == {"ok": True}


def test_entry_action_is_csrf_protected(browser):
    """The per-entry action routes are guarded too — a cookie post with no token is
    blocked before any work (so entry existence is irrelevant)."""
    _login(browser)
    r = browser.post(
        f"/erpsync/review/entries/{uuid.uuid4()}/approve", follow_redirects=False
    )
    assert r.status_code == 403
