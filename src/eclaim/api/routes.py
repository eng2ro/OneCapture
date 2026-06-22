"""e-Claim JSON API (spec §6)."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile

from ..auth.principal import Principal, list_visible_clients
from ..ocr.base import OcrError, OcrProvider
from ..services.claims import ClaimError, ClaimNotFound, ClaimService, IllegalTransition, Repos
from ..services.sod import SoDViolation
from . import deps
from .schemas import (
    AuditEventOut,
    BatchOut,
    ClaimDecision,
    ClaimEdit,
    ClaimOut,
    ClientOut,
    EntryOut,
    LedgerOut,
)

router = APIRouter(prefix="/api", tags=["eclaim"])
_service = ClaimService()

_SUPPORTED_MEDIA = {"image/jpeg", "image/png", "image/webp"}


def _handle(exc: ClaimError) -> HTTPException:
    if isinstance(exc, ClaimNotFound):
        return HTTPException(status_code=404, detail="claim not found")
    if isinstance(exc, SoDViolation):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, IllegalTransition):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/clients", response_model=list[ClientOut])
def list_clients(
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
) -> list[ClientOut]:
    """Clients the caller may see. Firm-scoped roles get the whole firm; a
    client-scoped role (Approver/Viewer) is narrowed to its granted clients —
    RLS only firm-gates this table, so the app layer does the per-client cut."""
    return [ClientOut.of(c) for c in list_visible_clients(repos.session, principal)]


@router.post("/claims/upload", response_model=ClaimOut, status_code=201)
async def upload_claim(
    file: UploadFile = File(...),
    claimant_ref: str | None = Form(default=None),
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
    ocr: OcrProvider = Depends(deps.get_ocr),
    image_dir: Path = Depends(deps.get_image_dir),
    spend_factor: Decimal = Depends(deps.get_spend_factor),
    actor: str = Depends(deps.get_actor),
) -> ClaimOut:
    media_type = file.content_type or "application/octet-stream"
    if media_type not in _SUPPORTED_MEDIA:
        raise HTTPException(status_code=415, detail=f"unsupported media type {media_type!r}")
    image_bytes = await file.read()
    try:
        claim = _service.upload(
            repos=repos,
            firm_id=principal.firm_id,
            client_id=deps.default_client_id(repos.session),
            image_bytes=image_bytes,
            media_type=media_type,
            ocr=ocr,
            image_dir=image_dir,
            spend_factor=spend_factor,
            actor=actor,
            claimant_ref=claimant_ref,
        )
    except OcrError as exc:
        raise HTTPException(status_code=422, detail=f"could not read receipt: {exc}")
    return ClaimOut.of(claim)


@router.get("/claims", response_model=list[ClaimOut])
def list_claims(
    status: str | None = None, repos: Repos = Depends(deps.get_repos)
) -> list[ClaimOut]:
    client_id = deps.default_client_id(repos.session)
    return [ClaimOut.of(c) for c in repos.claims.list(client_id, status)]


EXPORT_COLUMNS = [
    "claim_id", "doc_date", "status", "claimant_name", "employee_ref", "cost_centre",
    "vendor", "doc_no", "category_name", "gl_export_code", "currency", "total_amount",
    "scope", "basis", "tco2e", "factor_key", "release_batch_id",
]


def _parse_export_date(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"invalid {field}: {value!r} (expected ISO date/datetime)",
        )


# Registered BEFORE /claims/{claim_id} so "export" is not parsed as a claim UUID.
@router.get("/claims/export")
def export_claims(
    client_id: uuid.UUID | None = None,
    status: str = "released",
    date_from: str | None = None,
    date_to: str | None = None,
    batch_id: uuid.UUID | None = None,
    repos: Repos = Depends(deps.get_repos),
) -> Response:
    """CSV export of claims for the accounting system. RLS-scoped to the
    principal's clients; one row per matching claim. ``date_from``/``date_to``
    filter on the claim's capture timestamp (created_at)."""
    rows = repos.claims.export_rows(
        client_id=client_id,
        status=status,
        date_from=_parse_export_date(date_from, "date_from"),
        date_to=_parse_export_date(date_to, "date_to"),
        batch_id=batch_id,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        # Select column order matches EXPORT_COLUMNS; None → blank cell.
        writer.writerow(["" if v is None else str(v) for v in row])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="claims_export.csv"'},
    )


@router.get("/claims/{claim_id}", response_model=ClaimOut)
def get_claim(claim_id: uuid.UUID, repos: Repos = Depends(deps.get_repos)) -> ClaimOut:
    try:
        return ClaimOut.of(_service.get(repos, claim_id))
    except ClaimError as exc:
        raise _handle(exc)


@router.patch("/claims/{claim_id}", response_model=ClaimOut)
def edit_claim(
    claim_id: uuid.UUID,
    edit: ClaimEdit,
    repos: Repos = Depends(deps.get_repos),
    spend_factor: Decimal = Depends(deps.get_spend_factor),
    actor: str = Depends(deps.get_actor),
) -> ClaimOut:
    data = edit.model_dump(exclude_unset=True)
    category_id = data.pop("category_id", None)
    try:
        claim = _service.edit(
            repos=repos,
            claim_id=claim_id,
            fields=data,
            spend_factor=spend_factor,
            actor=actor,
            category_id=category_id,
        )
    except ClaimError as exc:
        raise _handle(exc)
    return ClaimOut.of(claim)


@router.post("/claims/{claim_id}/approve", response_model=ClaimOut)
def approve_claim(
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
    actor: str = Depends(deps.get_actor),
) -> ClaimOut:
    try:
        return ClaimOut.of(
            _service.approve(
                repos=repos, claim_id=claim_id, actor=actor, approver=principal
            )
        )
    except ClaimError as exc:
        raise _handle(exc)


@router.post("/claims/{claim_id}/send-back", response_model=ClaimOut)
def send_back_claim(
    claim_id: uuid.UUID,
    decision: ClaimDecision | None = None,
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
) -> ClaimOut:
    """Return an in-review claim to the submitter for rework (→ submitted)."""
    try:
        return ClaimOut.of(
            _service.send_back(
                repos=repos,
                claim_id=claim_id,
                reviewer=principal,
                reason=(decision.reason if decision else None),
            )
        )
    except ClaimError as exc:
        raise _handle(exc)


@router.post("/claims/{claim_id}/reject", response_model=ClaimOut)
def reject_claim(
    claim_id: uuid.UUID,
    decision: ClaimDecision | None = None,
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
) -> ClaimOut:
    """Reject an in-review claim outright (→ rejected, terminal)."""
    try:
        return ClaimOut.of(
            _service.reject(
                repos=repos,
                claim_id=claim_id,
                reviewer=principal,
                reason=(decision.reason if decision else None),
            )
        )
    except ClaimError as exc:
        raise _handle(exc)


@router.post("/claims/{claim_id}/resubmit", response_model=ClaimOut)
def resubmit_claim(
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
) -> ClaimOut:
    """Re-enter a sent-back claim into the review queue (→ in_review)."""
    try:
        return ClaimOut.of(
            _service.resubmit(
                repos=repos,
                claim_id=claim_id,
                actor=principal.email or str(principal.user_id),
            )
        )
    except ClaimError as exc:
        raise _handle(exc)


@router.post("/claims/{claim_id}/release", response_model=BatchOut)
def release_claim(
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_repos),
    actor: str = Depends(deps.get_actor),
) -> BatchOut:
    try:
        return BatchOut.of(_service.release(repos=repos, claim_id=claim_id, actor=actor))
    except ClaimError as exc:
        raise _handle(exc)


@router.post("/claims/{claim_id}/reverse", response_model=EntryOut)
def reverse_claim(
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_repos),
    actor: str = Depends(deps.get_actor),
) -> EntryOut:
    """Correct a released claim with a reversing (negative) ledger entry."""
    try:
        return EntryOut.of(_service.reverse(repos=repos, claim_id=claim_id, actor=actor))
    except ClaimError as exc:
        raise _handle(exc)


@router.get("/ledger", response_model=LedgerOut)
def ledger(repos: Repos = Depends(deps.get_repos)) -> LedgerOut:
    from ..repositories import LedgerRepository

    client_id = deps.default_client_id(repos.session)
    ledger_repo = LedgerRepository(repos.session)
    entries = ledger_repo.entries(client_id)
    totals = ledger_repo.scope_totals(client_id)
    s1, s2, s3 = totals.get(1, Decimal(0)), totals.get(2, Decimal(0)), totals.get(3, Decimal(0))
    return LedgerOut(
        entries=[EntryOut.of(e) for e in entries],
        scope_1=s1,
        scope_2=s2,
        scope_3=s3,
        total_tco2e=s1 + s2 + s3,
    )


@router.get("/audit/{claim_id}", response_model=list[AuditEventOut])
def audit_trail(
    claim_id: uuid.UUID, repos: Repos = Depends(deps.get_repos)
) -> list[AuditEventOut]:
    return [AuditEventOut.of(e) for e in repos.audit.chain("claim", claim_id)]
