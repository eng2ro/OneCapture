"""ERP Sync review (FR-S5): JSON API + server-rendered queue, in one app.

Mirrors e-Claim's shape — thin Jinja pages over the same repositories that POST
to a small JSON API — and runs under the same ``deps.get_repos`` /
``deps.get_principal`` plumbing, so every query is RLS-scoped and every mutation
is SoD-guarded under the live principal. Approve/dismiss/remap drive the review
service; the client-level release reuses the existing ``release_clean`` path.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict

from eclaim.api import deps
from eclaim.api.schemas import BatchOut
from eclaim.auth.principal import Principal, list_visible_clients
from eclaim.db.models import ErpsyncEntry
from eclaim.services.claims import Repos
from eclaim.web.routes import _nav_context
from erpsync.release.service import release_clean
from erpsync.review import service
from erpsync.review.leaf import carbon_leaf_state
from erpsync.review.service import (
    EntryNotFound,
    IllegalReviewState,
    RemapInput,
    ReviewError,
    ReviewSoDViolation,
)

# Reuse e-Claim's base.html / static styling by searching its template dir too.
# base.html (the shared shell) reads nav_counts / csrf_token / scope_name from the
# same _nav_context processor e-Claim registers — without it these pages raise
# UndefinedError on render, so wire it in here too (mirrors the e-Claim templates).
ECLAIM_WEB_TEMPLATES = Path(deps.__file__).resolve().parents[1] / "web" / "templates"
ERPSYNC_TEMPLATES = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(
    directory=[str(ERPSYNC_TEMPLATES), str(ECLAIM_WEB_TEMPLATES)],
    context_processors=[_nav_context],
)
# The carbon leaf is derived from BOTH the row's mapping and its releasability
# (punch-list R3), so both templates call this single source of truth rather than
# testing ``category != 'UNMAPPED'`` inline.
templates.env.globals["carbon_leaf_state"] = carbon_leaf_state


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class ErpsyncEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: uuid.UUID
    status: str
    doc_entry: str
    line_num: int
    doc_number: str | None
    category: str
    scope: str
    basis: str
    data_quality: str
    quantity: Decimal | None
    uom: str | None
    amount: Decimal | None
    factor_ref: str
    tco2e: Decimal
    edited_by_user_id: uuid.UUID | None
    reviewed_by_user_id: uuid.UUID | None
    review_note: str | None

    @classmethod
    def of(cls, entry) -> "ErpsyncEntryOut":
        return cls.model_validate(entry)


class ApproveBody(BaseModel):
    note: str | None = None


class RemapBody(BaseModel):
    category: str
    scope: str
    basis: str
    factor_ref: str
    factor_value: Decimal
    quantity: Decimal | None = None
    uom: str | None = None
    amount: Decimal | None = None


class DismissBody(BaseModel):
    # 'duplicate' confirms a dedup hold (audit: rejected_duplicate); else dismissed.
    reason: str = "other"
    note: str | None = None


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #
def _handle(exc: ReviewError) -> HTTPException:
    if isinstance(exc, EntryNotFound):
        return HTTPException(status_code=404, detail="entry not found")
    if isinstance(exc, ReviewSoDViolation):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, IllegalReviewState):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------- #
# JSON API
# --------------------------------------------------------------------------- #
api_router = APIRouter(prefix="/api/erpsync", tags=["erpsync-review"])


@api_router.get("/queue", response_model=list[ErpsyncEntryOut])
def queue(
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
) -> list[ErpsyncEntryOut]:
    rows = service.review_queue(repos.session, principal.allowed_client_ids)
    return [ErpsyncEntryOut.of(r) for r in rows]


@api_router.post("/entries/{entry_id}/approve", response_model=ErpsyncEntryOut)
def approve_entry(
    entry_id: uuid.UUID,
    body: ApproveBody | None = None,
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
) -> ErpsyncEntryOut:
    try:
        entry = service.approve(
            repos.session,
            entry_id=entry_id,
            reviewer=principal,
            note=(body.note if body else None),
        )
    except ReviewError as exc:
        raise _handle(exc)
    return ErpsyncEntryOut.of(entry)


@api_router.post("/entries/{entry_id}/remap", response_model=ErpsyncEntryOut)
def remap_entry(
    entry_id: uuid.UUID,
    body: RemapBody,
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
) -> ErpsyncEntryOut:
    try:
        entry = service.remap(
            repos.session,
            entry_id=entry_id,
            mapping=RemapInput(**body.model_dump()),
            reviewer=principal,
        )
    except ReviewError as exc:
        raise _handle(exc)
    return ErpsyncEntryOut.of(entry)


@api_router.post("/entries/{entry_id}/dismiss", response_model=ErpsyncEntryOut)
def dismiss_entry(
    entry_id: uuid.UUID,
    body: DismissBody | None = None,
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
) -> ErpsyncEntryOut:
    body = body or DismissBody()
    event_type = "rejected_duplicate" if body.reason == "duplicate" else "dismissed"
    try:
        entry = service.dismiss(
            repos.session,
            entry_id=entry_id,
            reviewer=principal,
            event_type=event_type,
            note=body.note,
        )
    except ReviewError as exc:
        raise _handle(exc)
    return ErpsyncEntryOut.of(entry)


@api_router.post("/clients/{client_id}/release", response_model=BatchOut | None)
def release_client(
    client_id: uuid.UUID,
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
) -> BatchOut | None:
    """Project this client's releasable (clean + approved) rows into the ledger
    via the shared release path. Returns null when there's nothing to release."""
    if principal.base_role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot release")
    if not principal.can_access_client(client_id):
        raise HTTPException(status_code=403, detail="no grant to this client")
    batch = release_clean(
        repos.session,
        firm_id=principal.firm_id,
        client_id=client_id,
        actor=principal.email or str(principal.user_id),
    )
    return None if batch is None else BatchOut.of(batch)


# --------------------------------------------------------------------------- #
# Server-rendered pages + their cookie-authenticated action routes
#
# This is a BROWSER surface: a logged-in reviewer carries the oc_session cookie, not
# a bearer token, so the pages AND the button actions authenticate by cookie
# (get_session_principal / get_web_repos) — the bearer JSON API under /api/erpsync is
# for programmatic clients. The router is CSRF-protected (csrf_protect is a no-op on
# GETs and on cookie-less requests), so the page buttons post a token-bearing fetch
# and a cross-site page can't drive approve/dismiss/remap/release (punch-list P7).
# --------------------------------------------------------------------------- #
web_router = APIRouter(tags=["erpsync-web"], dependencies=[Depends(deps.csrf_protect)])


@web_router.get("/erpsync/review", response_class=HTMLResponse)
def review_queue_page(
    request: Request,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    rows = service.review_queue(repos.session, principal.allowed_client_ids)
    names = {c.id: c.name for c in list_visible_clients(repos.session, principal)}
    # Group held/flagged rows by client for the queue view.
    groups: dict[uuid.UUID, list] = {}
    for row in rows:
        groups.setdefault(row.client_id, []).append(row)
    grouped = [(names.get(cid, str(cid)), entries) for cid, entries in groups.items()]
    return templates.TemplateResponse(
        request, "erpsync_queue.html", {"groups": grouped, "total": len(rows)}
    )


@web_router.get("/erpsync/entries/{entry_id}/review", response_class=HTMLResponse)
def entry_review_page(
    request: Request,
    entry_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    entry = repos.session.get(ErpsyncEntry, entry_id)
    events = repos.audit.chain("erpsync_entry", entry_id)
    return templates.TemplateResponse(
        request, "erpsync_entry.html", {"entry": entry, "events": events}
    )


@web_router.post("/erpsync/review/entries/{entry_id}/approve")
def web_approve_entry(
    entry_id: uuid.UUID,
    body: ApproveBody | None = None,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> dict:
    try:
        service.approve(
            repos.session, entry_id=entry_id, reviewer=principal,
            note=(body.note if body else None),
        )
    except ReviewError as exc:
        raise _handle(exc)
    return {"ok": True}


@web_router.post("/erpsync/review/entries/{entry_id}/remap")
def web_remap_entry(
    entry_id: uuid.UUID,
    body: RemapBody,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> dict:
    try:
        service.remap(
            repos.session, entry_id=entry_id,
            mapping=RemapInput(**body.model_dump()), reviewer=principal,
        )
    except ReviewError as exc:
        raise _handle(exc)
    return {"ok": True}


@web_router.post("/erpsync/review/entries/{entry_id}/dismiss")
def web_dismiss_entry(
    entry_id: uuid.UUID,
    body: DismissBody | None = None,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> dict:
    body = body or DismissBody()
    event_type = "rejected_duplicate" if body.reason == "duplicate" else "dismissed"
    try:
        service.dismiss(
            repos.session, entry_id=entry_id, reviewer=principal,
            event_type=event_type, note=body.note,
        )
    except ReviewError as exc:
        raise _handle(exc)
    return {"ok": True}


@web_router.post("/erpsync/review/clients/{client_id}/release")
def web_release_client(
    client_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> dict:
    if principal.base_role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot release")
    if not principal.can_access_client(client_id):
        raise HTTPException(status_code=403, detail="no grant to this client")
    release_clean(
        repos.session, firm_id=principal.firm_id, client_id=client_id,
        actor=principal.email or str(principal.user_id),
    )
    return {"ok": True}
