"""Web pages: Capture, Claims inbox, Review, Ledger. Server-rendered views over
the same services as the JSON API.

The inbox and review pages read through the repositories (RLS-scoped via the
request principal); the review actions POST to thin handlers here that call
:class:`ClaimService` and redirect back. The service stays the real gate — the
SoD/authority guard runs on approve/send-back/reject regardless of which buttons
the page chose to draw.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..api import deps
from ..auth.principal import Principal, list_visible_clients
from ..auth.provider import AuthError, DevAuthProvider
from ..config import get_settings
from ..db.models import Claimant
from ..repositories import LedgerRepository
from ..services.claims import ClaimError, ClaimService, Repos
from ..services.sod import can_approve

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

router = APIRouter(tags=["web"])
_service = ClaimService()

CLAIM_STATUSES = ["submitted", "in_review", "approved", "released", "rejected"]


def _actor(principal: Principal) -> str:
    return principal.email or str(principal.user_id)


@router.get("/", response_class=HTMLResponse)
def capture_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "capture.html", {})


# --------------------------------------------------------------------------- #
# Browser session login (cookie carrying the same signed token as the bearer API)
# --------------------------------------------------------------------------- #
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/login")
def web_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(""),
    db: Session = Depends(deps.get_db),
):
    """Authenticate via the same DevAuthProvider as POST /auth/login, then set the
    session cookie and redirect to the inbox. On failure, re-render with an error
    and set no cookie."""
    settings = get_settings()
    provider = DevAuthProvider(
        db, secret=settings.jwt_secret, ttl_seconds=settings.jwt_ttl_seconds
    )
    try:
        token = provider.login(email, password or None)
    except AuthError as exc:
        return templates.TemplateResponse(request, "login.html", {"error": str(exc)})
    resp = RedirectResponse("/claims", status_code=303)
    resp.set_cookie(
        deps.SESSION_COOKIE,
        token,
        max_age=settings.jwt_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return resp


@router.post("/logout")
def web_logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(deps.SESSION_COOKIE, path="/")
    return resp


# --------------------------------------------------------------------------- #
# Claims inbox
# --------------------------------------------------------------------------- #
@router.get("/claims", response_class=HTMLResponse)
def claims_inbox(
    request: Request,
    status: str | None = None,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    """One list serves inbox / approvals / flagged / posted — filtered by status,
    scoped to the principal's visible clients."""
    claims = repos.claims.list_for_clients(principal.allowed_client_ids, status)
    client_names = {c.id: c.name for c in list_visible_clients(repos.session, principal)}
    return templates.TemplateResponse(
        request,
        "claims.html",
        {
            "claims": claims,
            "client_names": client_names,
            "statuses": CLAIM_STATUSES,
            "current_status": status,
        },
    )


# --------------------------------------------------------------------------- #
# Review / detail
# --------------------------------------------------------------------------- #
def _render_review(
    request: Request,
    repos: Repos,
    principal: Principal,
    claim_id: uuid.UUID,
    error: str | None = None,
) -> HTMLResponse:
    claim = repos.claims.get(claim_id)
    if claim is None:
        return templates.TemplateResponse(request, "review.html", {"claim": None})
    category = repos.categories.get_by_id(claim.category_id) if claim.category_id else None
    claimant = (
        repos.session.get(Claimant, claim.submitted_by_claimant_id)
        if claim.submitted_by_claimant_id
        else None
    )
    can_edit = (
        principal.base_role != "viewer"
        and principal.can_access_client(claim.client_id)
        and claim.status != "released"
    )
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "claim": claim,
            "category": category,
            "claimant": claimant,
            "categories": repos.categories.list_for_client(claim.client_id),
            "events": repos.audit.chain("claim", claim_id),
            "can_review": can_approve(claim, principal) and claim.status == "in_review",
            "can_edit": can_edit,
            "can_resubmit": can_edit and claim.status == "submitted",
            "can_release": claim.status == "approved" and principal.base_role != "viewer",
            "error": error,
        },
    )


@router.get("/claims/{claim_id}/review", response_class=HTMLResponse)
def review_page(
    request: Request,
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    return _render_review(request, repos, principal, claim_id)


@router.get("/claims/{claim_id}/image")
def claim_image(claim_id: uuid.UUID, repos: Repos = Depends(deps.get_web_repos)):
    """Serve the stored receipt image (RLS-scoped: invisible claim → 404)."""
    claim = repos.claims.get(claim_id)
    if claim is None or not claim.image_path or not os.path.exists(claim.image_path):
        raise HTTPException(status_code=404, detail="image not available")
    return FileResponse(claim.image_path)


# --------------------------------------------------------------------------- #
# Review actions (thin wrappers over ClaimService; the service is the gate)
# --------------------------------------------------------------------------- #
def _action(request, repos, principal, claim_id, fn) -> HTMLResponse | RedirectResponse:
    try:
        fn()
    except ClaimError as exc:
        repos.session.rollback()
        return _render_review(request, repos, principal, claim_id, error=str(exc))
    return RedirectResponse(f"/claims/{claim_id}/review", status_code=303)


@router.post("/claims/{claim_id}/edit")
def web_edit(
    request: Request,
    claim_id: uuid.UUID,
    vendor: str = Form(""),
    doc_no: str = Form(""),
    doc_date: str = Form(""),
    currency: str = Form(""),
    total_amount: str = Form(""),
    expense_type: str = Form(""),
    quantity: str = Form(""),
    unit: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
    spend_factor: Decimal = Depends(deps.get_spend_factor),
):
    fields: dict = {}
    for key, value in (
        ("vendor", vendor), ("doc_no", doc_no), ("doc_date", doc_date),
        ("currency", currency), ("expense_type", expense_type), ("unit", unit),
    ):
        if value != "":
            fields[key] = value
    if total_amount != "":
        fields["total_amount"] = Decimal(total_amount)
    if quantity != "":
        fields["quantity"] = Decimal(quantity)
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.edit(
            repos=repos, claim_id=claim_id, fields=fields,
            spend_factor=spend_factor, actor=_actor(principal),
        ),
    )


@router.post("/claims/{claim_id}/category")
def web_assign_category(
    request: Request,
    claim_id: uuid.UUID,
    category_id: str = Form(...),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
    spend_factor: Decimal = Depends(deps.get_spend_factor),
):
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.edit(
            repos=repos, claim_id=claim_id, fields={},
            spend_factor=spend_factor, actor=_actor(principal),
            category_id=uuid.UUID(category_id),
        ),
    )


@router.post("/claims/{claim_id}/approve")
def web_approve(
    request: Request,
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.approve(
            repos=repos, claim_id=claim_id, actor=_actor(principal), approver=principal
        ),
    )


@router.post("/claims/{claim_id}/send-back")
def web_send_back(
    request: Request,
    claim_id: uuid.UUID,
    reason: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.send_back(
            repos=repos, claim_id=claim_id, reviewer=principal, reason=reason or None
        ),
    )


@router.post("/claims/{claim_id}/reject")
def web_reject(
    request: Request,
    claim_id: uuid.UUID,
    reason: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.reject(
            repos=repos, claim_id=claim_id, reviewer=principal, reason=reason or None
        ),
    )


@router.post("/claims/{claim_id}/resubmit")
def web_resubmit(
    request: Request,
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.resubmit(repos=repos, claim_id=claim_id, actor=_actor(principal)),
    )


@router.post("/claims/{claim_id}/release")
def web_release(
    request: Request,
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
    actor: str = Depends(deps.get_actor),
):
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.release(repos=repos, claim_id=claim_id, actor=actor),
    )


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
@router.get("/ledger", response_class=HTMLResponse)
def ledger_page(request: Request, repos: Repos = Depends(deps.get_web_repos)) -> HTMLResponse:
    client_id = deps.default_client_id(repos.session)
    ledger_repo = LedgerRepository(repos.session)
    entries = ledger_repo.entries(client_id)
    totals = ledger_repo.scope_totals(client_id)
    s1, s2, s3 = totals.get(1, Decimal(0)), totals.get(2, Decimal(0)), totals.get(3, Decimal(0))
    return templates.TemplateResponse(
        request,
        "ledger.html",
        {
            "entries": entries,
            "scope_1": s1,
            "scope_2": s2,
            "scope_3": s3,
            "total": s1 + s2 + s3,
        },
    )
