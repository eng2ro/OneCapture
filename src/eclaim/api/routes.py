"""e-Claim JSON API (spec §6)."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile

from ..auth.principal import Principal, list_visible_clients
from ..ocr.base import OcrError, OcrProvider
from ..services import erp as erp_service
from ..services import routing
from ..services.documents import normalize_image
from ..services.ingestion import _FormOcr
from ..services.claims import ClaimError, ClaimNotFound, ClaimService, IllegalTransition, Repos
from ..services.evidence import EvidenceService
from ..services.evidence_pdf import render as render_evidence_pdf
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

# HEIC/HEIF (iPhone) accepted and transcoded to JPEG in add_line before OCR/storage.
_SUPPORTED_MEDIA = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


def _claim_out(repos: Repos, claim) -> ClaimOut:
    """ClaimOut for one claim, loading its lines (the per-receipt records)."""
    return ClaimOut.of(claim, repos.claims.lines(claim.id))


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
    attested: bool = Form(default=False),
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
    ocr: OcrProvider = Depends(deps.get_ocr),
    image_dir: Path = Depends(deps.get_image_dir),
    actor: str = Depends(deps.get_actor),
) -> ClaimOut:
    if principal.base_role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot submit claims")
    media_type = file.content_type or "application/octet-stream"
    if media_type not in _SUPPORTED_MEDIA:
        raise HTTPException(status_code=415, detail=f"unsupported media type {media_type!r}")
    image_bytes = await file.read()
    try:
        norm_bytes, norm_media = normalize_image(image_bytes, media_type)
        extraction = ocr.extract(norm_bytes, norm_media)
    except (OcrError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"could not read receipt: {exc}")
    # /claims/upload is the staff-EXPENSE endpoint. If the classifier is confident this
    # is a vendor bill / delivery order, REFUSE it rather than silently filing it as an
    # expense claim (F2) — the caller should route it through the AP intake instead.
    if routing.route(extraction.document_type, extraction.type_confidence).queue != routing.QUEUE_ECLAIM:
        raise HTTPException(
            status_code=422,
            detail=(
                f"this document was classified as {extraction.document_type!r}, not a "
                "staff expense — capture it via the app's document intake, not "
                "/claims/upload"
            ),
        )
    try:
        # Reuse the already-read extraction (no second, billed OCR call).
        claim = _service.upload(
            repos=repos,
            firm_id=principal.firm_id,
            client_id=deps.default_client_id(repos.session),
            image_bytes=norm_bytes,
            media_type=norm_media,
            ocr=_FormOcr(extraction),
            image_dir=image_dir,
            actor=actor,
            claimant_ref=claimant_ref,
            attested=attested,
        )
    except (OcrError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"could not read receipt: {exc}")
    return _claim_out(repos, claim)


@router.get("/claims", response_model=list[ClaimOut])
def list_claims(
    status: str | None = None, repos: Repos = Depends(deps.get_repos)
) -> list[ClaimOut]:
    client_id = deps.default_client_id(repos.session)
    claims = repos.claims.list(client_id, status)
    lines = repos.claims.lines_by_claim([c.id for c in claims])
    return [ClaimOut.of(c, lines.get(c.id, [])) for c in claims]


# ERP reimbursement export — one row per APPROVED line (all classes; the carbon
# split is on the Carbon Next side). No tCO2e/scope here.
EXPORT_COLUMNS = [
    "claim_id", "line_no", "doc_date", "claim_status", "line_status",
    "claimant_name", "employee_ref", "cost_centre", "vendor", "doc_no",
    "category_name", "gl_code", "payment_method", "reimbursable",
    "currency", "total_amount", "tax_amount", "tax_code", "net_amount",
    "fx_rate", "base_amount", "posting_date", "department", "project_code",
    "supplier_tax_id", "carbon_relevant", "release_batch_id",
]

# Numeric columns whose leading '-' is a real negative sign, never an injection —
# excluded from the CSV formula-neutraliser applied to every free-text column.
_EXPORT_NUMERIC_COLS = frozenset(
    EXPORT_COLUMNS.index(c) for c in (
        "total_amount", "tax_amount", "net_amount", "fx_rate", "base_amount",
    )
)


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
    return Response(
        content=render_claims_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="claims_export.csv"'},
    )


def render_claims_csv(rows) -> str:
    """Build the claims CSV (shared by the bearer API export and the cookie-authed
    web export). Free-text cells (vendor, doc_no, names, codes — OCR/attacker-
    controlled) are neutralised against spreadsheet formula injection; numeric
    columns are left as-is so a legitimate leading '-' negative is not mangled."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([
            "" if v is None
            else str(v) if i in _EXPORT_NUMERIC_COLS
            else erp_service._csv_safe(str(v))
            for i, v in enumerate(row)
        ])
    return buf.getvalue()


@router.get("/claims/{claim_id}", response_model=ClaimOut)
def get_claim(claim_id: uuid.UUID, repos: Repos = Depends(deps.get_repos)) -> ClaimOut:
    try:
        return _claim_out(repos, _service.get(repos, claim_id))
    except ClaimError as exc:
        raise _handle(exc)


@router.patch("/claims/{claim_id}", response_model=ClaimOut)
def edit_claim(
    claim_id: uuid.UUID,
    edit: ClaimEdit,
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
    actor: str = Depends(deps.get_actor),
) -> ClaimOut:
    data = edit.model_dump(exclude_unset=True)
    category_id = data.pop("category_id", None)
    try:
        claim = _service.edit(
            repos=repos,
            claim_id=claim_id,
            fields=data,
            actor=actor,
            category_id=category_id,
            principal=principal,
        )
    except ClaimError as exc:
        raise _handle(exc)
    return _claim_out(repos, claim)


@router.post("/claims/{claim_id}/approve", response_model=ClaimOut)
def approve_claim(
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
    actor: str = Depends(deps.get_actor),
) -> ClaimOut:
    try:
        return _claim_out(
            repos,
            _service.approve(
                repos=repos, claim_id=claim_id, actor=actor, approver=principal
            ),
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
        return _claim_out(
            repos,
            _service.send_back(
                repos=repos,
                claim_id=claim_id,
                reviewer=principal,
                reason=(decision.reason if decision else None),
            ),
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
        return _claim_out(
            repos,
            _service.reject(
                repos=repos,
                claim_id=claim_id,
                reviewer=principal,
                reason=(decision.reason if decision else None),
            ),
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
        return _claim_out(
            repos,
            _service.resubmit(
                repos=repos,
                claim_id=claim_id,
                actor=principal.email or str(principal.user_id),
                principal=principal,   # else the viewer/writer gate is skipped
            ),
        )
    except ClaimError as exc:
        raise _handle(exc)


@router.post("/claims/{claim_id}/attest", response_model=ClaimOut)
def attest_claim(
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
) -> ClaimOut:
    """Record the claimant's out-of-pocket attestation on an existing claim so a
    pre-P3 claim carrying NULL attestation can clear the release gate (punch-list
    R2). Idempotency: attesting an already-attested claim is a 409, not a silent
    overwrite of the original declaration.

    Attestation is a personal declaration, so it is attributed to the AUTHENTICATED
    principal — NEVER the anonymous ``default_releaser`` (punch-list F1): a forgeable
    "system" attester would defeat the whole control. The service's writer gate (no
    viewers, must hold the client grant) decides who MAY attest."""
    actor = principal.email or str(principal.user_id)
    try:
        return _claim_out(
            repos,
            _service.attest(
                repos=repos, claim_id=claim_id, actor=actor, principal=principal
            ),
        )
    except ClaimError as exc:
        raise _handle(exc)


@router.post("/claims/{claim_id}/release", response_model=BatchOut)
def release_claim(
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
) -> BatchOut:
    # Attribute the release to the real caller (not the anonymous "system" actor)
    # and gate on role; this is a downstream sign-off to CarbonNext/ERP.
    actor = principal.email or str(principal.user_id)
    try:
        return BatchOut.of(
            _service.release(repos=repos, claim_id=claim_id, actor=actor, principal=principal)
        )
    except ClaimError as exc:
        raise _handle(exc)


@router.post("/claims/{claim_id}/reverse", response_model=BatchOut)
def reverse_claim(
    claim_id: uuid.UUID,
    reason: str = "",
    repos: Repos = Depends(deps.get_repos),
    principal: Principal = Depends(deps.get_principal),
) -> BatchOut:
    """Correct a released claim with a reversing (negative-quantity) batch.
    ``reason`` is required when the client's ``carbon.auto_reverse`` setting is
    ``approver_reason``; it rides the audit event."""
    actor = principal.email or str(principal.user_id)
    try:
        return BatchOut.of(
            _service.reverse(repos=repos, claim_id=claim_id, actor=actor,
                             principal=principal, reason=reason or None)
        )
    except ClaimError as exc:
        raise _handle(exc)


@router.get("/ledger", response_model=LedgerOut)
def ledger(repos: Repos = Depends(deps.get_repos)) -> LedgerOut:
    from ..repositories import LedgerRepository

    client_id = deps.default_client_id(repos.session)
    ledger_repo = LedgerRepository(repos.session)
    entries = ledger_repo.entries(client_id)
    counts = ledger_repo.direction_counts(client_id)
    forwarded, reversed_ = counts.get("forward", 0), counts.get("reversal", 0)
    return LedgerOut(
        entries=[EntryOut.of(e) for e in entries],
        forwarded=forwarded,
        reversed=reversed_,
        total_records=forwarded + reversed_,
    )


@router.get("/audit/{claim_id}", response_model=list[AuditEventOut])
def audit_trail(
    claim_id: uuid.UUID, repos: Repos = Depends(deps.get_repos)
) -> list[AuditEventOut]:
    return [AuditEventOut.of(e) for e in repos.audit.chain("claim", claim_id)]


@router.get("/claims/{claim_id}/evidence")
def claim_evidence(
    claim_id: uuid.UUID, repos: Repos = Depends(deps.get_repos)
) -> Response:
    """Regenerable per-claim evidence pack as a PDF (RLS-scoped). Assembles from
    stored data, then renders — so it can be regenerated identically any time."""
    try:
        evidence = EvidenceService.build(repos, claim_id)
    except ClaimError as exc:
        raise _handle(exc)
    pdf = render_evidence_pdf(evidence, datetime.now(timezone.utc))
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="evidence_{claim_id}.pdf"'},
    )
