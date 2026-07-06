"""Login brute-force throttle + generic errors (HIGH).

Unit-tests the sliding-window limiter with an injected clock (deterministic, no
sleeps), then drives the real web /login to prove the wiring: generic failure
message (no account enumeration) and a 429 once the limit is hit.
"""

from __future__ import annotations

import pytest

from eclaim.auth.ratelimit import LoginRateLimiter, RateLimited

SEED_EMAIL = "partner@seed.test"


# --- limiter unit tests (deterministic clock) -------------------------------
def _limiter(max_attempts, window, t):
    return LoginRateLimiter(max_attempts=max_attempts, window_seconds=window, now=lambda: t[0])


def test_blocks_after_max_then_expires_with_window():
    t = [1000.0]
    lim = _limiter(3, 100, t)
    for _ in range(3):
        lim.check("1.2.3.4", "a@x")          # allowed
        lim.record_failure("1.2.3.4", "a@x")
    with pytest.raises(RateLimited) as ei:
        lim.check("1.2.3.4", "a@x")          # 4th attempt blocked
    assert ei.value.retry_after > 0
    t[0] += 101                              # the whole window elapses
    lim.check("1.2.3.4", "a@x")              # allowed again — no raise


def test_email_bucket_blocks_across_ips():
    t = [0.0]
    lim = _limiter(2, 100, t)
    lim.record_failure("ip1", "a@x")
    lim.record_failure("ip2", "a@x")         # same email, different IP
    with pytest.raises(RateLimited):
        lim.check("ip3", "a@x")              # blocked by the email bucket


def test_success_clears_email_but_not_ip():
    t = [0.0]
    lim = _limiter(2, 100, t)
    lim.record_failure("ip1", "a@x")
    lim.record_success("ip1", "a@x")         # clears the email bucket only
    lim.check("ip1", "a@x")                  # email reset → allowed
    lim.record_failure("ip1", "b@y")         # ip1 now has 2 failures total
    with pytest.raises(RateLimited):
        lim.check("ip1", "z@z")             # blocked by the IP bucket (not cleared)


# --- web /login wiring ------------------------------------------------------
def test_web_login_is_generic_and_throttled(browser):
    r = browser.post("/login", data={"email": "nobody@nowhere.test"}, follow_redirects=False)
    assert r.status_code == 401
    assert "Sign in failed" in r.text
    assert "unknown user" not in r.text          # no account enumeration

    blocked = None
    for _ in range(15):                          # default cap is 10 → trips within 15
        r = browser.post("/login", data={"email": "nobody@nowhere.test"}, follow_redirects=False)
        if r.status_code == 429:
            blocked = r
            break
    assert blocked is not None, "login was never rate-limited"
    assert "Too many" in blocked.text
