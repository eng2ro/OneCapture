"""Pluggable authentication providers.

``DevAuthProvider`` is a local signed-token login for dev/tests; it looks a
seeded user up by email and mints a session token. ``EntraOIDCProvider`` is the
Phase-2 seam behind the same interface — declared, not implemented, so wiring
real Entra ID later doesn't touch callers.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import tokens


class AuthError(Exception):
    """Authentication failed (unknown/inactive user, bad credentials)."""


class AuthProvider(Protocol):
    """Mints a session token for a verified firm user."""

    def login(self, email: str, password: str | None = None) -> str: ...


# A SECURITY DEFINER function (created in the migration) lets the unprivileged
# app role look a user up by email *before* firm context exists — without it,
# RLS on app_user would hide every row at login time. It exposes only the
# minimal login fields.
_LOOKUP = text(
    "SELECT id, firm_id, base_role, status FROM auth_lookup_user(:email)"
)


class DevAuthProvider:
    """Local login: verify a seeded, active user and mint an HMAC-signed token.

    No password check in dev (identity is the seeded directory). The token
    carries ``user_id`` + ``firm_id`` + ``base_role`` per the spec.
    """

    def __init__(
        self,
        session: Session,
        *,
        secret: str,
        ttl_seconds: int,
        allow_passwordless: bool = True,
    ) -> None:
        self._session = session
        self._secret = secret
        self._ttl = ttl_seconds
        # When False (production), passwordless identity-only login is refused —
        # the deployment must front this with a real credential/SSO provider.
        self._allow_passwordless = allow_passwordless

    def login(self, email: str, password: str | None = None) -> str:
        if not self._allow_passwordless:
            raise AuthError(
                "passwordless dev login is disabled in production — "
                "configure a real identity provider (Entra/OIDC)"
            )
        # Every write path stores emails lowercased (users service, seed), but the
        # SECURITY DEFINER lookup matches exactly — normalise the probe so a user
        # typing Their@Email.Com still signs in.
        rows = self._session.execute(_LOOKUP, {"email": (email or "").strip().lower()}).all()
        if not rows:
            raise AuthError("unknown user")
        if len(rows) > 1:
            raise AuthError("ambiguous user (email not unique across firms)")
        user_id, firm_id, base_role, status = rows[0]
        if status != "active":
            raise AuthError("inactive user")
        return tokens.mint(
            {"user_id": str(user_id), "firm_id": str(firm_id), "base_role": base_role},
            secret=self._secret,
            ttl_seconds=self._ttl,
        )


class EntraOIDCProvider:
    """Phase-2 seam: Microsoft Entra ID (OIDC). Not implemented in this spine."""

    def login(self, email: str, password: str | None = None) -> str:  # pragma: no cover
        raise NotImplementedError("Entra OIDC is a Phase-2 seam (DevAuthProvider only)")
