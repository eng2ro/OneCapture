"""Auth endpoints. ``POST /auth/login`` mints a session token (DevAuthProvider).

Login runs on a raw session with no tenant context — it must find the user
before the firm is known, which the SECURITY DEFINER lookup behind
``DevAuthProvider`` allows.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.session import get_session
from .provider import AuthError, DevAuthProvider
from .ratelimit import RateLimited, client_ip

router = APIRouter(prefix="/auth", tags=["auth"])

# One opaque message for every failure so a probe can't tell "no such user" from
# "wrong password" (no account enumeration). The specific reason is logged/raised
# inside the provider for server-side diagnostics, never returned to the client.
_GENERIC_LOGIN_ERROR = "invalid email or password"


class LoginRequest(BaseModel):
    email: str
    password: str | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest, request: Request, session: Session = Depends(get_session)
) -> LoginResponse:
    settings = get_settings()
    limiter = request.app.state.login_limiter
    ip = client_ip(request)
    email = (body.email or "").strip().lower()
    try:
        limiter.check(ip, email)
    except RateLimited as rl:
        raise HTTPException(
            status_code=429,
            detail="too many login attempts — please wait and try again",
            headers={"Retry-After": str(rl.retry_after)},
        ) from rl

    provider = DevAuthProvider(
        session, secret=settings.jwt_secret, ttl_seconds=settings.jwt_ttl_seconds,
        allow_passwordless=settings.dev_auth_allowed,
    )
    try:
        token = provider.login(body.email, body.password)
    except AuthError as exc:
        limiter.record_failure(ip, email)
        raise HTTPException(status_code=401, detail=_GENERIC_LOGIN_ERROR) from exc
    limiter.record_success(ip, email)
    return LoginResponse(access_token=token)
