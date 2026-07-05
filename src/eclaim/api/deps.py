"""Request-scoped dependencies: DB session/transaction, repos, OCR, client.

The session dependency commits on a clean return and rolls back on any
exception, so each request is one atomic unit. ``get_ocr`` returns the real
Anthropic provider by default; tests override it via ``app.dependency_overrides``
to inject a fake — the API never calls the model in CI.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.principal import Principal, build_principal
from ..auth.tokens import TokenError, verify
from ..config import get_settings
from ..db.models import Client
from ..db.session import get_sessionmaker
from ..ocr.anthropic_provider import AnthropicVisionProvider
from ..ocr.base import OcrProvider
from ..ocr.segment import AnthropicPageSegmenter, PageSegmenter
from ..services.claims import Repos
from ..tenancy import set_firm_context, set_tenant_context


def get_db() -> Iterator[Session]:
    db = get_sessionmaker()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_principal(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Principal:
    """Validate the bearer token and resolve the request principal.

    Sets ``app.current_firm`` first so the firm-scoped tables are visible while
    the user + grants load; the full client context is set later by
    :func:`get_repos`. Overridden in tests to inject a Principal directly.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = verify(token, secret=get_settings().jwt_secret)
        set_firm_context(db, uuid.UUID(str(claims["firm_id"])))
        return build_principal(db, claims)
    except (TokenError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc


def get_repos(
    db: Session = Depends(get_db), principal: Principal = Depends(get_principal)
) -> Repos:
    """Apply the principal's tenant context to the session, then build repos so
    every query in the request runs under RLS."""
    set_tenant_context(db, principal.firm_id, principal.allowed_client_ids)
    return Repos.for_session(db)


# --------------------------------------------------------------------------- #
# Browser session (cookie) auth — for the server-rendered web pages.
# --------------------------------------------------------------------------- #
SESSION_COOKIE = "oc_session"


class NeedsLogin(Exception):
    """A web request without a valid session cookie. The app handles it with a
    redirect to /login (vs the API's bearer 401), so a browser is sent to sign in
    rather than getting an error."""


class WebForbidden(Exception):
    """A logged-in web user reached a page their role may not use (the firm-scope
    admin area). The app renders a friendly in-shell 'no access' page (vs the API's
    bare 403 JSON), so a browser gets a readable message instead of a raw error.
    The string value is the human-facing reason."""


def get_session_principal(
    request: Request, db: Session = Depends(get_db)
) -> Principal:
    """Resolve the principal from the ``oc_session`` cookie — the SAME signed
    token the bearer path uses, just carried in a cookie. Raises
    :class:`NeedsLogin` (→ redirect) when missing/invalid; the API's bearer
    ``get_principal`` is untouched."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise NeedsLogin()
    try:
        claims = verify(token, secret=get_settings().jwt_secret)
        set_firm_context(db, uuid.UUID(str(claims["firm_id"])))
        principal = build_principal(db, claims)
    except (TokenError, KeyError, ValueError) as exc:
        raise NeedsLogin() from exc
    # Stash the principal + session on request.state so the nav context processor
    # can render the sidebar (Admin section, live badge counts, tenant scope)
    # without threading either through every page.
    request.state.principal = principal
    request.state.db = db
    return principal


def get_web_repos(
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_session_principal),
) -> Repos:
    """Cookie-sourced counterpart to :func:`get_repos` for the web pages."""
    set_tenant_context(db, principal.firm_id, principal.allowed_client_ids)
    return Repos.for_session(db)


def require_firm_scope(
    principal: Principal = Depends(get_session_principal),
) -> Principal:
    """Admin web routes are firm-scope only (partner/manager). Approver/viewer are
    sent to a friendly 'no access' page via WebForbidden; a cookie-less request
    still redirects to /login via get_session_principal."""
    if not principal.is_firm_scoped:
        raise WebForbidden(
            "The Manage tools (events, claimants and categories) are open to "
            "partners and managers. Ask one of them if you need a change here."
        )
    return principal


def get_ocr() -> OcrProvider:
    return AnthropicVisionProvider()


def get_segmenter() -> PageSegmenter:
    """LLM page-segmenter for multi-invoice PDFs (Phase 4). Tests override this."""
    return AnthropicPageSegmenter()


def get_directions():
    """Server-side Google Directions provider (authoritative mileage distance).
    Raises MapError at call time if no key is configured; tests override this."""
    from ..maps import GoogleDirectionsProvider

    return GoogleDirectionsProvider(get_settings().google_maps_api_key)


def get_mileage_rate() -> Decimal:
    return Decimal(get_settings().mileage_rate_per_km)


def get_image_dir() -> Path:
    return get_settings().image_dir


def get_spend_factor() -> Decimal:
    return Decimal(get_settings().spend_factor)


def get_actor() -> str:
    return get_settings().default_releaser


def default_client_id(db: Session) -> uuid.UUID:
    """Resolve the single firm's client id (multi-tenant scoping is deferred)."""
    client = db.execute(select(Client).order_by(Client.created_at).limit(1)).scalar_one()
    return client.id
