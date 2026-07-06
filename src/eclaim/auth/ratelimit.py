"""In-process login rate limiter (HIGH: brute-force protection).

Login has no throttle, so an attacker can try unlimited email/password guesses.
This sliding-window limiter caps failed attempts per **IP** and per **email**
(both buckets — an IP hammering many accounts is caught by the IP bucket; a
password-spray hitting one account from many IPs by the email bucket).

Scope: process-local (a dict guarded by a lock), which fits the single-host pilot
and the app's current single-worker deployment. A multi-process / multi-host
deployment should back this with Postgres or Redis — the ``LoginRateLimiter``
interface (check / record_failure / record_success) is the seam to swap.

The clock is injectable (``now``) so tests are deterministic without sleeping;
it defaults to ``time.monotonic`` (immune to wall-clock changes).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import Request


class RateLimited(Exception):
    """Raised when a login attempt is over the limit. ``retry_after`` is a whole
    number of seconds until the oldest counted attempt ages out of the window."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("too many login attempts")
        self.retry_after = retry_after


class LoginRateLimiter:
    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._now = now
        self._ip: dict[str, deque[float]] = defaultdict(deque)
        self._email: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, dq: deque[float], cutoff: float) -> None:
        while dq and dq[0] <= cutoff:
            dq.popleft()

    def check(self, ip: str, email: str) -> None:
        """Raise :class:`RateLimited` if either the IP or the email bucket is already
        at the limit. Call before attempting authentication."""
        now = self._now()
        cutoff = now - self._window
        with self._lock:
            for bucket, key in ((self._ip, ip), (self._email, email)):
                dq = bucket.get(key)
                if not dq:
                    continue
                self._prune(dq, cutoff)
                if not dq:
                    del bucket[key]  # keep the maps from growing unbounded
                    continue
                if len(dq) >= self._max:
                    raise RateLimited(max(int(self._window - (now - dq[0])) + 1, 1))

    def record_failure(self, ip: str, email: str) -> None:
        now = self._now()
        with self._lock:
            self._ip[ip].append(now)
            self._email[email].append(now)

    def record_success(self, ip: str, email: str) -> None:
        """Clear the email bucket on a genuine sign-in (a user who mistyped a couple
        of times isn't left near the limit). The IP bucket is deliberately NOT
        cleared — else an attacker who owns one valid account could reset the IP
        limit and keep spraying — it ages out via the sliding window instead."""
        with self._lock:
            self._email.pop(email, None)


def client_ip(request: Request) -> str:
    """The direct peer address. We intentionally do NOT trust ``X-Forwarded-For``
    here: with no authenticated reverse proxy in front yet (deferred B8), an
    attacker could spoof that header to dodge the per-IP limit. Wire XFF in only
    once a trusted proxy sets it."""
    return request.client.host if request.client else "unknown"
