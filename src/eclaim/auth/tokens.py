"""Minimal HMAC-signed session token (JWT-shaped) for ``DevAuthProvider``.

A real deployment uses Entra ID / a vetted JWT library; this is a dependency-free
stand-in for local dev and tests. Format is ``base64url(header).base64url(payload).
base64url(HMAC-SHA256)`` — recognisably a JWT (HS256) but minted/verified here so
the spine has no new dependency. The signature covers header+payload, so tampering
or an expired ``exp`` is rejected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class TokenError(Exception):
    """Token is malformed, has a bad signature, or has expired."""


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _sign(signing_input: bytes, secret: str) -> str:
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return _b64u_encode(sig)


def mint(claims: dict, *, secret: str, ttl_seconds: int, now: float | None = None) -> str:
    """Mint a signed token carrying ``claims`` plus ``iat``/``exp``."""
    issued = int(now if now is not None else time.time())
    payload = {**claims, "iat": issued, "exp": issued + ttl_seconds}
    header = {"alg": "HS256", "typ": "JWT"}
    header_seg = _b64u_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_seg = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    return f"{header_seg}.{payload_seg}.{_sign(signing_input, secret)}"


def verify(token: str, *, secret: str, now: float | None = None) -> dict:
    """Validate signature + expiry and return the payload claims, else raise."""
    try:
        header_seg, payload_seg, sig_seg = token.split(".")
    except ValueError as exc:
        raise TokenError("malformed token") from exc

    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    expected = _sign(signing_input, secret)
    if not hmac.compare_digest(expected, sig_seg):
        raise TokenError("bad signature")

    try:
        payload = json.loads(_b64u_decode(payload_seg))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError("malformed payload") from exc

    clock = now if now is not None else time.time()
    if int(payload.get("exp", 0)) < clock:
        raise TokenError("token expired")
    return payload
