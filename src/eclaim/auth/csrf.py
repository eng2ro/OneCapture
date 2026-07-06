"""Stateless CSRF token bound to the browser session cookie.

The web UI authenticates with the ``oc_session`` JWT cookie, which the browser
attaches automatically on every request to this origin — the exact condition a
cross-site request forgery exploits. We defend with a *session-bound* token:

    token = base64url(HMAC-SHA256(jwt_secret, "csrf:" + session_jwt))

It is unforgeable without the server secret and tied to the specific session, so
an attacker's cross-site form — which can neither read the victim's session
cookie (HttpOnly, and cross-origin script can't reach it) nor the secret — can
never carry the right value. Being *derived* from the session it needs no store
and no second cookie: the server recomputes it from the presented session cookie
and compares in constant time.

Bearer-authenticated API routes need none of this and are deliberately left
untouched: a browser never attaches an ``Authorization`` header to a cross-site
request, so they are CSRF-immune by construction. See :func:`deps.csrf_protect`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

# Domain separation: keep this HMAC use distinct from any other keyed by the same
# secret (the session token's own signature in tokens.py), so one can never be
# substituted for the other.
_PREFIX = b"csrf:"


def issue(session_token: str, *, secret: str) -> str:
    """The CSRF token for a given session cookie value."""
    mac = hmac.new(
        secret.encode("utf-8"), _PREFIX + session_token.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def valid(session_token: str, submitted: object, *, secret: str) -> bool:
    """True iff ``submitted`` is the token bound to ``session_token``. Non-str or
    missing submissions are rejected; the comparison is constant-time."""
    if not isinstance(submitted, str) or not submitted:
        return False
    return hmac.compare_digest(issue(session_token, secret=secret), submitted)
