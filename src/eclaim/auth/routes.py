"""Auth endpoints. ``POST /auth/login`` mints a session token (DevAuthProvider).

Login runs on a raw session with no tenant context — it must find the user
before the firm is known, which the SECURITY DEFINER lookup behind
``DevAuthProvider`` allows.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.session import get_session
from .provider import AuthError, DevAuthProvider

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, session: Session = Depends(get_session)) -> LoginResponse:
    settings = get_settings()
    provider = DevAuthProvider(
        session, secret=settings.jwt_secret, ttl_seconds=settings.jwt_ttl_seconds,
        allow_passwordless=settings.dev_auth_allowed,
    )
    try:
        token = provider.login(body.email, body.password)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return LoginResponse(access_token=token)
