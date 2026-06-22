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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import get_args

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..api import deps
from ..auth.principal import Principal, list_visible_clients
from ..auth.provider import AuthError, DevAuthProvider
from ..config import get_settings
from ..db.models import Category, Claimant
from ..ocr.base import Extraction, ExpenseType, OcrError, Unit
from ..repositories import LedgerRepository
from ..services.claims import ClaimError, ClaimService, Repos
from ..services.sod import can_approve

WEB_DIR = Path(__file__).parent


def _nav_context(request: Request) -> dict:
    """Inject ``is_firm_scoped`` into every page so the nav can show the Admin
    section only to partner/manager. Reads the principal stashed on request.state
    by get_session_principal (unset on unauthenticated pages → hidden)."""
    principal = getattr(request.state, "principal", None)
    return {"is_firm_scoped": bool(principal and principal.is_firm_scoped)}


templates = Jinja2Templates(
    directory=str(WEB_DIR / "templates"), context_processors=[_nav_context]
)

router = APIRouter(tags=["web"])
_service = ClaimService()

CLAIM_STATUSES = ["submitted", "in_review", "approved", "released", "rejected"]
EXPENSE_TYPES = get_args(ExpenseType)          # the fixed OCR expense vocabulary
UNITS = get_args(Unit)
SUPPORTED_MEDIA = {"image/jpeg", "image/png", "image/webp"}


def _actor(principal: Principal) -> str:
    return principal.email or str(principal.user_id)


class _FormOcr:
    """A manual-entry OcrProvider: returns the Extraction built from the capture
    form. Lets the cookie web path reuse ClaimService.upload unchanged (same
    classify/category/audit/image-store path) instead of forking it — the vision
    model is simply not invoked for a hand-keyed claim."""

    def __init__(self, extraction: Extraction) -> None:
        self._extraction = extraction

    def extract(self, image_bytes: bytes, media_type: str) -> Extraction:
        return self._extraction


@router.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/capture", status_code=307)


# --------------------------------------------------------------------------- #
# Capture (cookie-authed web entry point to ClaimService.upload)
# --------------------------------------------------------------------------- #
def _render_capture(request: Request, error: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "capture.html",
        {"expense_types": EXPENSE_TYPES, "units": UNITS, "error": error},
    )


@router.get("/capture", response_class=HTMLResponse)
def capture_page(
    request: Request,
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    return _render_capture(request)


@router.post("/capture")
async def web_capture(
    request: Request,
    file: UploadFile = File(...),
    expense_type: str = Form("other"),
    quantity: str = Form(""),
    unit: str = Form(""),
    total_amount: str = Form(""),
    vendor: str = Form(""),
    doc_no: str = Form(""),
    doc_date: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
    image_dir: Path = Depends(deps.get_image_dir),
    spend_factor: Decimal = Depends(deps.get_spend_factor),
):
    """Hand-keyed capture: build the Extraction from the form, then run the SAME
    ClaimService.upload the API uses. 303 to the new claim's review page."""
    media_type = file.content_type or "application/octet-stream"
    if media_type not in SUPPORTED_MEDIA:
        return _render_capture(request, f"unsupported image type {media_type!r}")
    image_bytes = await file.read()
    try:
        extraction = Extraction(
            vendor=vendor or None,
            doc_no=doc_no or None,
            date=doc_date or None,
            total_amount=Decimal(total_amount) if total_amount else None,
            expense_type=expense_type or "other",
            quantity=Decimal(quantity) if quantity else None,
            unit=unit or None,
        )
        claim = _service.upload(
            repos=repos,
            firm_id=principal.firm_id,
            client_id=deps.default_client_id(repos.session),
            image_bytes=image_bytes,
            media_type=media_type,
            ocr=_FormOcr(extraction),
            image_dir=image_dir,
            spend_factor=spend_factor,
            actor=_actor(principal),
        )
    except (ValidationError, InvalidOperation, OcrError, ClaimError) as exc:
        repos.session.rollback()
        return _render_capture(request, str(exc))
    return RedirectResponse(f"/claims/{claim.id}/review", status_code=303)


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


# --------------------------------------------------------------------------- #
# Admin: category + claimant master (firm-scope roles only; RLS-scoped)
# --------------------------------------------------------------------------- #
def _render_categories(request, repos, principal, *, editing=None, error=None) -> HTMLResponse:
    clients = list_visible_clients(repos.session, principal)
    return templates.TemplateResponse(
        request,
        "admin_categories.html",
        {
            "categories": repos.categories.list_for_clients([c.id for c in clients]),
            "clients": clients,
            "client_names": {c.id: c.name for c in clients},
            "expense_types": EXPENSE_TYPES,
            "editing": editing,
            "error": error,
        },
    )


@router.get("/admin/categories", response_class=HTMLResponse)
def admin_categories(
    request: Request,
    edit: uuid.UUID | None = None,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
) -> HTMLResponse:
    editing = repos.categories.get_by_id(edit) if edit else None
    return _render_categories(request, repos, principal, editing=editing)


@router.post("/admin/categories")
def admin_save_category(
    request: Request,
    category_id: str = Form(""),
    client_id: str = Form(...),
    name: str = Form(...),
    expense_type: str = Form(...),
    factor_key: str = Form(""),
    gl_export_code: str = Form(""),
    default_limit: str = Form(""),
    status: str = Form("active"),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
):
    try:
        cid = uuid.UUID(client_id)
        limit = Decimal(default_limit) if default_limit.strip() else None
    except (ValueError, InvalidOperation):
        return _render_categories(request, repos, principal, error="Invalid client or default limit.")
    if cid not in principal.allowed_client_ids:
        return _render_categories(request, repos, principal, error="You cannot manage that client.")
    try:
        with repos.session.begin_nested():
            if category_id.strip():
                cat = repos.categories.get_by_id(uuid.UUID(category_id))
                if cat is None or cat.client_id != cid:
                    raise LookupError
                cat.name, cat.expense_type = name, expense_type
                cat.factor_key, cat.gl_export_code = factor_key or None, gl_export_code or None
                cat.default_limit, cat.status = limit, status or "active"
            else:
                repos.session.add(
                    Category(
                        firm_id=principal.firm_id, client_id=cid, name=name,
                        expense_type=expense_type, factor_key=factor_key or None,
                        gl_export_code=gl_export_code or None, default_limit=limit,
                        status=status or "active",
                    )
                )
            repos.session.flush()
    except LookupError:
        return _render_categories(request, repos, principal, error="Category not found.")
    except IntegrityError:
        return _render_categories(
            request, repos, principal,
            error="A category with that name or expense type already exists for this client.",
        )
    return RedirectResponse("/admin/categories", status_code=303)


def _render_claimants(request, repos, principal, *, editing=None, error=None) -> HTMLResponse:
    clients = list_visible_clients(repos.session, principal)
    return templates.TemplateResponse(
        request,
        "admin_claimants.html",
        {
            "claimants": repos.claimants.list_for_clients([c.id for c in clients]),
            "clients": clients,
            "client_names": {c.id: c.name for c in clients},
            "editing": editing,
            "error": error,
        },
    )


@router.get("/admin/claimants", response_class=HTMLResponse)
def admin_claimants(
    request: Request,
    edit: uuid.UUID | None = None,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
) -> HTMLResponse:
    editing = repos.claimants.get_by_id(edit) if edit else None
    return _render_claimants(request, repos, principal, editing=editing)


@router.post("/admin/claimants")
def admin_save_claimant(
    request: Request,
    claimant_id: str = Form(""),
    client_id: str = Form(...),
    name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    employee_ref: str = Form(""),
    cost_centre: str = Form(""),
    status: str = Form("active"),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
):
    try:
        cid = uuid.UUID(client_id)
    except ValueError:
        return _render_claimants(request, repos, principal, error="Invalid client.")
    if cid not in principal.allowed_client_ids:
        return _render_claimants(request, repos, principal, error="You cannot manage that client.")
    try:
        with repos.session.begin_nested():
            if claimant_id.strip():
                cm = repos.claimants.get_by_id(uuid.UUID(claimant_id))
                if cm is None or cm.client_id != cid:
                    raise LookupError
                cm.name, cm.phone, cm.email = name, phone or None, email or None
                cm.employee_ref, cm.cost_centre = employee_ref or None, cost_centre or None
                cm.status = status or "active"
            else:
                repos.session.add(
                    Claimant(
                        firm_id=principal.firm_id, client_id=cid, name=name,
                        phone=phone or None, email=email or None,
                        employee_ref=employee_ref or None, cost_centre=cost_centre or None,
                        status=status or "active",
                    )
                )
            repos.session.flush()
    except LookupError:
        return _render_claimants(request, repos, principal, error="Claimant not found.")
    except IntegrityError:
        return _render_claimants(
            request, repos, principal,
            error="A claimant with that phone already exists for this client.",
        )
    return RedirectResponse("/admin/claimants", status_code=303)
