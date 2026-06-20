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

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.principal import Principal, build_principal
from ..auth.tokens import TokenError, verify
from ..config import get_settings
from ..db.models import Client
from ..db.session import get_sessionmaker
from ..ocr.anthropic_provider import AnthropicVisionProvider
from ..ocr.base import OcrProvider
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


def get_ocr() -> OcrProvider:
    return AnthropicVisionProvider()


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
