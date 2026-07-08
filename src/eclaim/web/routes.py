"""Web pages: Capture, Claims inbox, Review, Ledger. Server-rendered views over
the same services as the JSON API.

The inbox and review pages read through the repositories (RLS-scoped via the
request principal); the review actions POST to thin handlers here that call
:class:`ClaimService` and redirect back. The service stays the real gate — the
SoD/authority guard runs on approve/send-back/reject regardless of which buttons
the page chose to draw.
"""

from __future__ import annotations

import io
import json
import os
import uuid
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import get_args

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..api import deps
from ..auth import csrf
from ..auth.principal import Principal, list_visible_clients
from ..auth.provider import AuthError, DevAuthProvider
from ..auth.ratelimit import RateLimited, client_ip
from ..config import get_settings
from ..db.models import ApprovalMatrixRule, Category, Claimant, Client, Event, IngestionJob
from ..ocr.base import Extraction, ExpenseType, OcrError, OcrProvider, Unit
from ..ocr.segment import PageSegmenter
from ..repositories import ClaimRepository, LedgerRepository
from ..services import ap as ap_service
from ..services import coverage as coverage_service
from ..services import erp as erp_service
from ..services import fx as fx_service
from ..services import vehicles as vehicles_service
from ..services import payables as payables_service
from ..services import ingestion, routing
from ..services import intake as intake_service
from ..services.documents import normalize_image
from ..services.claims import CLAIM_TYPES, ClaimError, ClaimNotFound, ClaimService, Repos
from ..services.sod import SoDViolation, can_approve
from ..tenancy import set_tenant_context

WEB_DIR = Path(__file__).parent


def _scope_name(clients) -> str | None:
    """The label for the topbar/greeting: the single visible client's name, or a
    summary when a firm-scoped user sees several."""
    if not clients:
        return None
    if len(clients) == 1:
        return clients[0].name
    return f"All clients ({len(clients)})"


def _nav_context(request: Request) -> dict:
    """Inject the principal, role flag, live sidebar badge counts, and tenant
    scope name into every page so the shell renders real data instead of the
    mockup placeholders. Reads the principal + session stashed on request.state
    by get_session_principal (unauthenticated pages → bare defaults)."""
    principal = getattr(request.state, "principal", None)
    # Session-bound CSRF token for every rendered form / fetch on the page (bound to
    # the presented session cookie; empty on unauthenticated pages like /login).
    session_token = request.cookies.get(deps.SESSION_COOKIE)
    ctx: dict = {
        "principal": principal,
        "is_firm_scoped": bool(principal and principal.is_firm_scoped),
        "nav_counts": {},
        "nav_total": 0,
        "scope_name": None,
        "csrf_token": (
            csrf.issue(session_token, secret=get_settings().jwt_secret)
            if session_token
            else ""
        ),
    }
    db = getattr(request.state, "db", None)
    if principal is None or db is None:
        return ctx
    try:
        set_tenant_context(db, principal.firm_id, principal.allowed_client_ids)
        counts = ClaimRepository(db).status_counts(principal.allowed_client_ids)
        ctx["scope_name"] = _scope_name(list_visible_clients(db, principal))
        # Vendor-bills badge: pages the classifier parked in the holding queue (C1).
        # A COUNT, not a full load of every intake row on every page render (F9).
        ctx["intake_holding_count"] = intake_service.holding_count(
            db, principal.allowed_client_ids
        )
        # Approvals-inbox badge (Appendix E1): claims in review + AP bills awaiting
        # approval — COUNTs only, same rule as above.
        from sqlalchemy import func as _f, select as _sel

        from ..db.models import ApInvoice as _Ap

        ap_pending = 0
        if principal.allowed_client_ids:
            ap_pending = int(db.execute(
                _sel(_f.count()).select_from(_Ap).where(
                    _Ap.client_id.in_(principal.allowed_client_ids),
                    _Ap.status.in_(("coded", "pending_approval")),
                )
            ).scalar_one())
        ctx["approvals_count"] = (
            counts.get("submitted", 0) + counts.get("in_review", 0) + ap_pending
        )
    except Exception:  # nav chrome must never break a page render
        return ctx
    ctx["nav_total"] = sum(counts.values())   # total claims — sum BEFORE deriving group keys
    # Derived badge totals for the grouped sidebar filters (mirror STATUS_GROUPS).
    counts = dict(counts)
    counts["attention"] = counts.get("sent_back", 0) + counts.get("partially_approved", 0)
    counts["released_all"] = (
        counts.get("released", 0) + counts.get("exported", 0) + counts.get("paid", 0)
    )
    ctx["nav_counts"] = counts
    return ctx


templates = Jinja2Templates(
    directory=str(WEB_DIR / "templates"), context_processors=[_nav_context]
)

# Every state-changing web route is CSRF-guarded by this router-level dependency
# (fail-closed: covers current and future routes without per-handler wiring). It
# no-ops on safe methods and on non-cookie requests — see deps.csrf_protect.
router = APIRouter(tags=["web"], dependencies=[Depends(deps.csrf_protect)])
_service = ClaimService()

CLAIM_STATUSES = [
    "submitted", "in_review", "approved", "partially_approved",
    "sent_back", "released", "rejected", "exported", "paid",
]

# Sidebar filter groups: one menu item can span several lifecycle statuses.
#   attention → claims bounced back to the claimant to fix (queried / partially approved)
#   released  → anything that has left for accounting (released and everything after)
# A key not listed here is treated as a single exact status.
STATUS_GROUPS = {
    "attention": ["sent_back", "partially_approved"],
    "released": ["released", "exported", "paid"],
}
PAYMENT_METHODS = ["out_of_pocket", "corporate_card", "company_paid"]
EVENT_TYPES = ["training", "travel", "client_meeting", "conference", "team", "project", "other"]
EXPENSE_TYPES = get_args(ExpenseType)          # the fixed OCR expense vocabulary
UNITS = get_args(Unit)
# HEIC/HEIF (iPhone) accepted and transcoded to JPEG before OCR (documents.normalize_image).
SUPPORTED_MEDIA = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


def _actor(principal: Principal) -> str:
    return principal.email or str(principal.user_id)


def _opt_uuid(value: str | None) -> uuid.UUID | None:
    """Parse an optional id from a query string. A blank or malformed ``?edit=``
    means 'no record selected' (show the list) — never a 422. Typing the param as
    ``uuid.UUID | None`` would 422 on an empty string, which breaks the edit links
    if one ever renders without an id."""
    try:
        return uuid.UUID(value) if value and value.strip() else None
    except ValueError:
        return None


@router.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/capture", status_code=307)


# --------------------------------------------------------------------------- #
# Capture (cookie-authed web entry point to ClaimService.upload)
# --------------------------------------------------------------------------- #
def _capture_categories(repos: Repos) -> list[Category]:
    """The active expense categories the staff member chooses from on capture —
    the client's real list (Meals, Taxi, Fuel, ...), not the carbon vocabulary."""
    return repos.categories.list_for_client(deps.default_client_id(repos.session))


def _category_json(categories: list[Category]) -> list[dict]:
    """Shape the categories for the capture page's JS: enough to drive the
    suggested-category pick and a quiet carbon hint. ``carbon_relevant`` = this
    category's spend is forwarded to CarbonNext (e-Claim does no carbon maths)."""
    return [
        {
            "id": str(c.id),
            "name": c.name,
            "expense_type": c.expense_type,
            "carbon_relevant": c.carbon_relevant,
        }
        for c in categories
    ]


def _events_for(repos: Repos) -> list:
    """Active events the staff member can attach a claim to (capture/review)."""
    return repos.events.list_for_clients([deps.default_client_id(repos.session)])


def _render_capture(
    request: Request,
    categories: list[Category],
    events: list | None = None,
    error: str | None = None,
    form: dict | None = None,
) -> HTMLResponse:
    resp = templates.TemplateResponse(
        request,
        "capture.html",
        {
            "categories": categories,
            "categories_json": _category_json(categories),
            "events": events or [],
            "claim_types": CLAIM_TYPES,
            "payment_methods": PAYMENT_METHODS,
            "units": UNITS,
            # Mileage mode: browser Maps key (referrer-restricted) + per-km rate.
            "maps_key": get_settings().google_maps_browser_key or get_settings().google_maps_api_key,
            "mileage_rate": get_settings().mileage_rate_per_km,
            # Registered vehicles for the mileage vehicle picker (Appendix H-C).
            "vehicles": vehicles_service.list_for_clients(
                request.state.db, list(request.state.principal.allowed_client_ids),
                active_only=True,
            ) if hasattr(request.state, "principal") else [],
            "error": error,
            # Echo the header fields back on a validation error so the user does
            # not have to re-pick the type/dates (the receipts are re-dropped).
            "form": form or {},
        },
    )
    # The capture page's inline JS carries the classifier verdict (document_type) in the
    # POST payload; a browser serving a STALE cached copy would drop it and mis-file
    # vendor bills as expenses. Never cache this page, so its JS is always current.
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@router.get("/capture", response_class=HTMLResponse)
def capture_page(
    request: Request,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    return _render_capture(request, _capture_categories(repos), _events_for(repos))


@router.post("/capture/extract")
async def capture_extract(
    request: Request,
    file: UploadFile = File(...),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
    ocr: OcrProvider = Depends(deps.get_ocr),
) -> JSONResponse:
    """Read a receipt image and return its extracted fields as JSON, WITHOUT
    creating a claim — the capture page calls this on upload so staff can verify
    the auto-captured data before submitting. Degrades gracefully: any read
    failure (incl. no API key configured) returns ``{"ok": false}`` with a
    friendly reason so the page falls back to manual entry instead of breaking."""
    media_type = file.content_type or "application/octet-stream"
    if media_type not in SUPPORTED_MEDIA:
        return JSONResponse(
            {"ok": False, "reason": f"Unsupported file type ({media_type})."},
            status_code=415,
        )
    image_bytes = await file.read()
    try:
        # iPhone HEIC → JPEG before OCR (the vision API doesn't read HEIC).
        image_bytes, media_type = normalize_image(image_bytes, media_type, name=file.filename or "")
        extraction = ocr.extract(image_bytes, media_type)
    except (OcrError, ValueError) as exc:
        configured = bool(get_settings().anthropic_api_key)
        reason = (
            "Couldn't read this receipt automatically — please enter the details."
            if configured
            else "Auto-capture isn't configured yet — please enter the details."
        )
        return JSONResponse({"ok": False, "reason": reason, "detail": str(exc)})
    suggested = repos.categories.match_by_merchant(
        deps.default_client_id(repos.session), extraction.vendor, extraction.expense_type
    )
    return JSONResponse(
        {
            "ok": True,
            "extraction": extraction.model_dump(mode="json"),
            "suggested_category_id": str(suggested.id) if suggested else None,
        }
    )


def _submission_needs_attestation(item_list, mileage_specs, file_count: int) -> bool:
    """Does this capture contain out-of-pocket EXPENSE that requires the declaration?

    True when there's a mileage trip, an uploaded file with no classifying item
    (server-OCR of an unknown type — treated as a potential expense), or any pre-read
    item the classifier routes to e-Claim. False only when EVERY item is a vendor bill /
    delivery order (they divert to the AP holding queue and create no claim), so a
    pure-vendor-bill upload doesn't force the tick. Server-side mirror of the capture
    page's own gate, so a crafted POST can't bypass it."""
    if any(
        isinstance(s, dict) and str(s.get("origin") or "").strip()
        and str(s.get("destination") or "").strip()
        for s in mileage_specs
    ):
        return True
    data_items = [i for i in item_list if ingestion.item_has_data(i)]
    # A file with no data-bearing item is an unread/server-OCR page of unknown type.
    if file_count > len(data_items):
        return True
    for item in data_items:
        dt = item.get("document_type") or "expense_receipt"
        if routing.route(dt, item.get("type_confidence")).queue == routing.QUEUE_ECLAIM:
            return True
    return False


@router.post("/capture")
async def web_capture(
    request: Request,
    files: list[UploadFile] = File(default=[]),
    items: str = Form("[]"),
    mileage: str = Form("[]"),
    title: str = Form(""),
    purpose: str = Form(""),
    remarks: str = Form(""),
    posting_date: str = Form(""),
    claim_type: str = Form("general"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    event_id: str = Form(""),
    new_event_title: str = Form(""),
    new_event_start: str = Form(""),
    new_event_end: str = Form(""),
    attested: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
    image_dir: Path = Depends(deps.get_image_dir),
    ocr: OcrProvider = Depends(deps.get_ocr),
    segmenter: PageSegmenter = Depends(deps.get_segmenter),
):
    """Capture one claim from a batch of receipts: ONE ``in_review`` claim whose
    LINES are the dropped receipts. A small upload is read inline (instant); a large
    multi-invoice upload — which is dozens of slow vision reads — is staged and
    handed to the background ingestion worker, and the browser goes to a progress
    page instead of hanging on the request. Both paths share
    :func:`ingestion.build_claim`, so the resulting claim is identical."""
    try:
        item_list = json.loads(items) if items.strip() else []
    except json.JSONDecodeError:
        item_list = []
    try:
        mileage_specs = json.loads(mileage) if mileage.strip() else []
    except json.JSONDecodeError:
        mileage_specs = []
    if not isinstance(mileage_specs, list):
        mileage_specs = []

    client_id = deps.default_client_id(repos.session)
    _client = repos.session.get(Client, client_id)
    split_docs = bool(_client and (_client.modules or {}).get("allow_document_split"))

    is_attested = attested.strip().lower() in ("1", "true", "yes", "on")
    header = {
        "title": title, "purpose": purpose, "remarks": remarks,
        "posting_date": posting_date, "claim_type": claim_type,
        "start_date": start_date, "end_date": end_date, "event_id": event_id,
        "new_event_title": new_event_title, "new_event_start": new_event_start,
        "new_event_end": new_event_end, "actor": _actor(principal),
        "attested": is_attested,
    }
    # Echoed back into the form if inline validation fails (receipts get re-dropped).
    form = {
        "title": title, "claim_type": claim_type, "purpose": purpose,
        "remarks": remarks, "posting_date": posting_date,
        "start_date": start_date, "end_date": end_date, "event_id": event_id,
        "attested": is_attested,
    }

    # Reject an unreasonable number of file parts up front (blocker B7) — the byte
    # cap bounds total size, this bounds fan-out (each file is a slow vision read).
    max_files = get_settings().max_upload_files
    named_files = [f for f in files if (f.filename or "").strip()]
    if len(named_files) > max_files:
        return _render_capture(
            request, _capture_categories(repos), _events_for(repos),
            f"Too many files — please upload at most {max_files} at once "
            f"(you selected {len(named_files)}).",
            form,
        )

    # Out-of-pocket attestation (Appendix A): the declaration is required ONLY when the
    # submission actually contains out-of-pocket EXPENSE — a mileage trip, or a receipt
    # the classifier routes to e-Claim. An all-vendor-bill upload diverts to the AP
    # holding queue and creates no reimbursement claim, so it needs no declaration (a
    # vendor bill is not something you paid out of pocket). Computed from the item
    # payload's classification BEFORE the read phase, so a missing tick still costs no
    # OCR; the release-time gate remains the real control for anything that slips
    # through. The stamp itself is recorded in ClaimService.submit.
    if _submission_needs_attestation(item_list, mileage_specs, len(named_files)) and not is_attested:
        return _render_capture(
            request, _capture_categories(repos), _events_for(repos),
            "Please confirm the out-of-pocket declaration before submitting your claim.",
            form,
        )

    # Slurp the uploads into memory once (skip empty file parts — an empty selection
    # posts a part with no filename, e.g. a mileage-only claim).
    staged: list[dict] = []
    for f in named_files:
        staged.append({
            "name": f.filename,
            "media_type": f.content_type or "application/octet-stream",
            "bytes": await f.read(),
        })

    # Large batches read too slowly to do in the request — stage + enqueue for the
    # background worker and send the browser to the progress page.
    if ingestion.estimate_units(staged) > ingestion.INLINE_MAX_UNITS:
        job_id = uuid.uuid4()
        manifest = ingestion.stage_files(image_dir, job_id, staged)
        ingestion.enqueue_job(
            repos, job_id=job_id, firm_id=principal.firm_id, client_id=client_id,
            created_by_user_id=principal.user_id,
            allowed_client_ids=principal.allowed_client_ids,
            header=header, item_list=item_list, mileage_specs=mileage_specs,
            split_docs=split_docs, manifest=manifest,
            total_estimate=ingestion.estimate_units(staged),
        )
        return RedirectResponse(f"/ingest/{job_id}", status_code=303)

    # Small upload: build the claim inline for an instant result.
    providers = ingestion.Providers(
        ocr=ocr, segmenter=segmenter, image_dir=image_dir,
        mileage_rate=deps.get_mileage_rate(), directions=deps.get_directions(),
    )
    result = ingestion.build_claim(
        repos, providers, firm_id=principal.firm_id, client_id=client_id,
        created_by_user_id=principal.user_id,
        allowed_client_ids=principal.allowed_client_ids,
        header=header, staged=staged, item_list=item_list,
        mileage_specs=mileage_specs, split_docs=split_docs,
    )
    if result.header_error:
        return _render_capture(
            request, _capture_categories(repos), _events_for(repos), result.header_error, form
        )
    if result.added == 0 and result.diverted:
        # Every page was a vendor bill / delivery order — the classifier routed them to
        # the "Vendor bills (coming soon)" holding queue instead of forcing a claim.
        return RedirectResponse("/intake/holding?routed=1", status_code=303)
    if result.added == 0:
        msg = "Could not add any line. " + ingestion.summarize_errors(result.errors)
        return _render_capture(request, _capture_categories(repos), _events_for(repos), msg)
    dest = f"/claims/{result.claim_id}/review"
    if result.diverted:
        dest += f"?diverted={result.diverted}"      # some pages went to the holding queue
    return RedirectResponse(dest, status_code=303)


# --------------------------------------------------------------------------- #
# Async ingestion progress (a large upload is built by the background worker)
# --------------------------------------------------------------------------- #
@router.get("/ingest/{job_id}", response_class=HTMLResponse, response_model=None)
def ingest_progress(
    request: Request,
    job_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse | RedirectResponse:
    """Progress page for an async capture. Redirects to the review screen once the
    worker has built the claim; RLS scopes the job to the caller's firm/client."""
    job = repos.session.get(IngestionJob, job_id)
    if job is None:
        return RedirectResponse("/capture", status_code=303)
    if job.status == "done" and job.claim_id:
        return RedirectResponse(f"/claims/{job.claim_id}/review", status_code=303)
    return templates.TemplateResponse(request, "ingesting.html", {"job": job})


@router.get("/ingest/{job_id}/status")
def ingest_status(
    job_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> dict:
    """JSON the progress page polls: state, read count, and where to go when done."""
    return _job_status_dict(repos.session, repos.session.get(IngestionJob, job_id))


def _job_status_dict(session, job: IngestionJob | None) -> dict:
    if job is None:
        return {"state": "unknown"}
    redirect = None
    if job.status == "done":
        # A done job with no claim means every page was a vendor bill / DO that the
        # classifier diverted to the intake holding queue (C1) — send them there.
        # A MIXED job (claim + diverted pages) carries ?diverted=N so the review
        # page shows the same "N page(s) went to Vendor bills" banner as the sync
        # path — without it the async split was silent.
        if job.claim_id:
            from sqlalchemy import func as _func, select as _select

            from ..db.models import DocumentIntake as _DI

            diverted = session.execute(
                _select(_func.count()).select_from(_DI)
                .where(_DI.ingestion_job_id == job.id)
            ).scalar_one()
            redirect = f"/claims/{job.claim_id}/review" + (
                f"?diverted={diverted}" if diverted else ""
            )
        else:
            redirect = "/intake/holding?routed=1"
    return {
        "state": job.status, "done": job.done_units, "total": job.total_units,
        "error": job.error, "redirect": redirect,
    }


@router.get("/ingest/{job_id}/events")
def ingest_events(
    job_id: uuid.UUID,
    principal: Principal = Depends(deps.get_session_principal),
) -> StreamingResponse:
    """Server-Sent Events stream of a job's progress — the progress page prefers this
    to polling. Uses its own short-lived session (re-scoped each read) so a fresh
    Postgres snapshot picks up the worker's committed updates; caps at ~10 min."""
    from ..db.session import get_sessionmaker

    def _events():
        import json as _json
        import time

        session = get_sessionmaker()()
        try:
            for _ in range(600):
                session.rollback()   # end any txn → next read is a fresh snapshot
                set_tenant_context(session, principal.firm_id, principal.allowed_client_ids)
                payload = _job_status_dict(session, session.get(IngestionJob, job_id))
                yield f"data: {_json.dumps(payload)}\n\n"
                if payload.get("redirect") or payload["state"] in ("failed", "unknown"):
                    break
                time.sleep(1.0)
        finally:
            session.close()

    return StreamingResponse(_events(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# Intake holding queue (C1): vendor bills / delivery orders the classifier routed
# off the e-Claim path, captured now and processed when the AP module ships. A
# reviewer can correct a mis-route ("this is actually a …").
# --------------------------------------------------------------------------- #
@router.get("/intake/holding", response_class=HTMLResponse)
def intake_holding(
    request: Request,
    routed: str = "",
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    """The 'Vendor bills (coming soon)' queue: AP-side + still-undecided pages awaiting
    the AP module or a manual route. Capture now, process later — never silently forced
    into e-Claim."""
    rows = intake_service.holding_queue(repos.session, principal.allowed_client_ids)
    return templates.TemplateResponse(
        request, "intake_holding.html",
        {"rows": rows, "just_routed": bool(routed), "can_act": principal.base_role != "viewer"},
    )


@router.post("/intake/{intake_id}/reroute")
def intake_reroute(
    request: Request,
    intake_id: uuid.UUID,
    to: str = Form(...),
    claim_id: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
    image_dir: Path = Depends(deps.get_image_dir),
    ocr: OcrProvider = Depends(deps.get_ocr),
):
    """A reviewer's correction of a route (C1). Re-routing to e-Claim either
    APPENDS the page to an existing pre-approval claim (``claim_id`` given — the
    review page's "bring it back" button, no re-OCR, no vendor-module detour) or
    re-runs the e-Claim builder from the stored image as a new single-line claim.
    The correction is audited either way. Viewers cannot re-route."""
    if principal.base_role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot re-route documents")
    try:
        row = intake_service.get_intake(repos.session, intake_id)
        if not principal.can_access_client(row.client_id):
            raise HTTPException(status_code=403, detail="no grant to this client")

        target_claim_id = None
        if to == routing.QUEUE_ECLAIM:
            existing = _opt_uuid(claim_id)
            if existing is not None:
                _service.attach_intake_as_line(
                    repos=repos, claim_id=existing, intake=row,
                    actor=_actor(principal), principal=principal,
                )
                target_claim_id = existing
            else:
                if not row.image_path or not Path(row.image_path).exists():
                    raise ClaimError("the captured image is no longer available to re-file")
                image_bytes = Path(row.image_path).read_bytes()
                claim = _service.upload(
                    repos=repos, firm_id=principal.firm_id, client_id=row.client_id,
                    image_bytes=image_bytes, media_type=row.media_type or "image/jpeg",
                    ocr=ocr, image_dir=image_dir, actor=_actor(principal),
                )
                target_claim_id = claim.id
        intake_service.reroute(
            repos.session, intake_id=intake_id, to=to,
            actor=_actor(principal), claim_id=target_claim_id,
        )
        repos.session.commit()
    except intake_service.IntakeError as exc:
        repos.session.rollback()
        set_tenant_context(repos.session, principal.firm_id, principal.allowed_client_ids)
        status = 404 if isinstance(exc, intake_service.IntakeNotFound) else 409
        raise HTTPException(status_code=status, detail=str(exc))
    except (ClaimError, SoDViolation) as exc:
        repos.session.rollback()
        set_tenant_context(repos.session, principal.firm_id, principal.allowed_client_ids)
        raise HTTPException(status_code=400, detail=str(exc))
    if target_claim_id is not None:
        return RedirectResponse(f"/claims/{target_claim_id}/review", status_code=303)
    return RedirectResponse("/intake/holding", status_code=303)


@router.post("/intake/{intake_id}/file-ap")
def intake_file_ap(
    request: Request,
    intake_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """File a held vendor bill as an AP invoice (C1 → C2): resolve the vendor, seed the
    invoice + a line, and consume the intake. Viewers cannot."""
    if principal.base_role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot file invoices")
    try:
        intake = intake_service.get_intake(repos.session, intake_id)
        if not principal.can_access_client(intake.client_id):
            raise HTTPException(status_code=403, detail="no grant to this client")
        if intake.status == "consumed":
            raise HTTPException(status_code=409, detail="this page has already been processed")
        invoice = ap_service.create_from_intake(
            repos.session, intake=intake, actor=_actor(principal)
        )
        repos.session.commit()
    except IntegrityError:
        # TOCTOU: a concurrent request already filed this intake (its idempotency key
        # "intake:<id>" collided on uq_ap_invoice_idem) — treat as already-processed.
        repos.session.rollback()
        set_tenant_context(repos.session, principal.firm_id, principal.allowed_client_ids)
        raise HTTPException(status_code=409, detail="this page has already been filed")
    except ap_service.ApError as exc:
        repos.session.rollback()
        set_tenant_context(repos.session, principal.firm_id, principal.allowed_client_ids)
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(f"/ap/{invoice.id}", status_code=303)


# --------------------------------------------------------------------------- #
# AP invoices (C2): the vendor-bill workflow — capture → code → approve → export.
# --------------------------------------------------------------------------- #
@router.get("/ap", response_class=HTMLResponse)
def ap_list(
    request: Request,
    status: str = "",
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    invoices = ap_service.list_invoices(
        repos.session, principal.allowed_client_ids, status=status or None
    )
    vendors = {v.id: v for v in _ap_vendors(repos, principal)}
    return templates.TemplateResponse(
        request, "ap_list.html",
        {"invoices": invoices, "vendors": vendors, "status": status,
         "can_act": principal.base_role != "viewer"},
    )


@router.get("/ap/export.csv")
def ap_export_csv(
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Download the AP invoices an accountant still needs to post manually (C2 stub):
    approved or paid, not yet carrying an ERP key — paying first must never drop a
    bill out of the posting pipeline."""
    invoices = ap_service.exportable_invoices(
        repos.session, principal.allowed_client_ids
    )
    csv_text = erp_service.export_ap_csv(repos.session, invoices)
    from fastapi.responses import Response as _Response
    return _Response(
        content=csv_text, media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="ap_invoices.csv"'},
    )


@router.get("/ap/{invoice_id}", response_class=HTMLResponse)
def ap_detail(
    request: Request,
    invoice_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    try:
        invoice = ap_service.get_invoice(repos.session, invoice_id)
    except ap_service.ApNotFound:
        raise HTTPException(status_code=404, detail="invoice not found")
    if not principal.can_access_client(invoice.client_id):
        raise HTTPException(status_code=404, detail="invoice not found")
    lines = ap_service.lines(repos.session, invoice_id)
    vendor = repos.session.get(ap_service.Vendor, invoice.vendor_id)
    # Whether THIS principal could approve it (drives the button + SoD explanation).
    rule = ap_service.matrix_rule_for_invoice(repos.session, invoice)
    approve_block = None
    if invoice.status in ("coded", "pending_approval"):
        try:
            ap_service.check_can_approve_invoice(invoice, principal, matrix_rule=rule)
        except SoDViolation as exc:
            approve_block = str(exc)
    return templates.TemplateResponse(
        request, "ap_detail.html",
        {
            "invoice": invoice, "lines": lines, "vendor": vendor,
            "categories": repos.categories.list_for_client(invoice.client_id),
            "events": repos.audit.chain("ap_invoice", invoice_id),
            "can_act": principal.base_role != "viewer"
            and principal.can_access_client(invoice.client_id),
            "approve_block": approve_block,
        },
    )


@router.post("/ap/{invoice_id}/code")
def ap_code(
    request: Request,
    invoice_id: uuid.UUID,
    line_id: str = Form(...),
    gl_code: str = Form(""),
    tax_code: str = Form(""),
    category_id: str = Form(""),
    department: str = Form(""),
    project_code: str = Form(""),
    description: str = Form(""),
    quantity: str = Form(""),
    uom: str = Form(""),
    line_total: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    def _dec(v: str):
        try:
            return Decimal(v.strip()) if v.strip() else None
        except InvalidOperation:
            return None

    return _ap_action(
        request, repos, principal, invoice_id,
        lambda: ap_service.code_line(
            repos.session, line_id=uuid.UUID(line_id), coder=principal,
            actor=_actor(principal),
            gl_code=gl_code.strip() or None, tax_code=tax_code.strip() or None,
            category_id=uuid.UUID(category_id) if category_id.strip() else None,
            department=department.strip() or None, project_code=project_code.strip() or None,
            description=description.strip() or None,
            quantity=_dec(quantity), uom=uom.strip() or None, line_total=_dec(line_total),
        ),
    )


@router.post("/ap/{invoice_id}/header")
def ap_edit_header(
    request: Request,
    invoice_id: uuid.UUID,
    doc_no: str = Form(""),
    doc_date: str = Form(""),
    total_amount: str = Form(""),
    currency: str = Form(""),
    po_ref: str = Form(""),
    do_ref: str = Form(""),
    vendor_name: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Correct the OCR-read header (doc no / date / total / currency / refs / a
    freshly-minted vendor's name) — every field feeds the CarbonNext handoff or the
    duplicate-payment control (F-E item 12)."""
    import datetime as _dt

    def _dec(v: str):
        try:
            return Decimal(v.strip()) if v.strip() else None
        except InvalidOperation:
            return None

    parsed_date = None
    if doc_date.strip():
        try:
            parsed_date = _dt.date.fromisoformat(doc_date.strip())
        except ValueError:
            parsed_date = None
    return _ap_action(
        request, repos, principal, invoice_id,
        lambda: ap_service.edit_header(
            repos.session, invoice_id=invoice_id, editor=principal,
            actor=_actor(principal),
            doc_no=doc_no.strip() or None, doc_date=parsed_date,
            total_amount=_dec(total_amount),
            currency=(currency.strip().upper() or None),
            po_ref=po_ref.strip() or None, do_ref=do_ref.strip() or None,
            vendor_name=vendor_name.strip() or None,
        ),
    )


@router.post("/claims/{claim_id}/lines/{line_id}/to-vendor-bill")
def claim_line_to_vendor_bill(
    request: Request,
    claim_id: uuid.UUID,
    line_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """E3: this receipt is really a vendor bill — move it to the holding queue
    (pre-approval only; the switcher can't approve what it becomes)."""
    if principal.base_role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot modify claims")
    try:
        _service.switch_line_to_vendor_bill(
            repos=repos, claim_id=claim_id, line_id=line_id,
            actor=_actor(principal), principal=principal,
        )
        repos.session.commit()
    except (ClaimError, SoDViolation) as exc:
        repos.session.rollback()
        set_tenant_context(repos.session, principal.firm_id, principal.allowed_client_ids)
        status = 403 if isinstance(exc, SoDViolation) else 409
        raise HTTPException(status_code=status, detail=str(exc))
    return RedirectResponse("/intake/holding?routed=1", status_code=303)


@router.post("/claims/{claim_id}/lines/to-vendor-bills")
def claim_lines_to_vendor_bills(
    request: Request,
    claim_id: uuid.UUID,
    line_ids: list[str] = Form(default=[]),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """E3 (batch): move MANY selected receipt lines to Vendor bills at once — the
    supplier-invoice-dump case (e.g. 24 vendor lines filed as one expense claim)."""
    if principal.base_role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot modify claims")
    ids = [u for u in (_opt_uuid(x) for x in line_ids) if u is not None]
    if not ids:
        return RedirectResponse(f"/claims/{claim_id}/review", status_code=303)
    try:
        _service.switch_lines_to_vendor_bills(
            repos=repos, claim_id=claim_id, line_ids=ids,
            actor=_actor(principal), principal=principal,
        )
        repos.session.commit()
    except (ClaimError, SoDViolation) as exc:
        repos.session.rollback()
        set_tenant_context(repos.session, principal.firm_id, principal.allowed_client_ids)
        status = 403 if isinstance(exc, SoDViolation) else 409
        raise HTTPException(status_code=status, detail=str(exc))
    # A claim voided (all pages left) has nowhere to return to — send to the queue.
    claim = repos.claims.get(claim_id)
    if claim is not None and claim.status == "rejected":
        return RedirectResponse("/intake/holding?routed=1", status_code=303)
    return RedirectResponse(f"/claims/{claim_id}/review", status_code=303)


@router.post("/ap/{invoice_id}/to-expense")
def ap_to_expense(
    request: Request,
    invoice_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """E3: this vendor bill is really a staff expense — convert it to an
    in-review claim (pre-approval only)."""
    if principal.base_role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot modify invoices")
    try:
        claim = ap_service.switch_to_expense(
            repos.session, invoice_id=invoice_id, editor=principal,
            actor=_actor(principal),
        )
        repos.session.commit()
    except (ap_service.ApError, SoDViolation, ClaimError) as exc:
        repos.session.rollback()
        set_tenant_context(repos.session, principal.firm_id, principal.allowed_client_ids)
        if isinstance(exc, ap_service.ApNotFound):
            raise HTTPException(status_code=404, detail=str(exc))
        status = 403 if isinstance(exc, SoDViolation) else 409
        raise HTTPException(status_code=status, detail=str(exc))
    return RedirectResponse(f"/claims/{claim.id}/review", status_code=303)


@router.post("/ap/{invoice_id}/lines/add")
def ap_add_line(
    request: Request,
    invoice_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Add an empty line — how a reviewer SPLITS a lump-filed bill into its real
    lines so a mixed bill can forward only its carbon share (F-E item 11)."""
    return _ap_action(
        request, repos, principal, invoice_id,
        lambda: ap_service.add_line(
            repos.session, invoice_id=invoice_id, editor=principal,
            actor=_actor(principal), line=ap_service.LineInput(description="(new line)"),
        ),
    )


@router.post("/ap/{invoice_id}/lines/{line_id}/delete")
def ap_delete_line(
    request: Request,
    invoice_id: uuid.UUID,
    line_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    return _ap_action(
        request, repos, principal, invoice_id,
        lambda: ap_service.remove_line(
            repos.session, line_id=line_id, editor=principal, actor=_actor(principal),
        ),
    )


@router.post("/ap/{invoice_id}/submit")
def ap_submit(
    request: Request, invoice_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    return _ap_action(
        request, repos, principal, invoice_id,
        lambda: ap_service.submit_for_approval(
            repos.session, invoice_id=invoice_id, actor=_actor(principal),
            submitter=principal,
        ),
    )


@router.post("/ap/{invoice_id}/approve")
def ap_approve(
    request: Request, invoice_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    return _ap_action(
        request, repos, principal, invoice_id,
        lambda: ap_service.approve(
            repos.session, invoice_id=invoice_id, approver=principal, actor=_actor(principal)
        ),
    )


@router.post("/ap/{invoice_id}/release-hold")
def ap_release_hold(
    request: Request, invoice_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Clear a false-positive duplicate hold so the bill can proceed (F6)."""
    return _ap_action(
        request, repos, principal, invoice_id,
        lambda: ap_service.release_hold(
            repos.session, invoice_id=invoice_id, actor=_actor(principal)
        ),
    )


@router.post("/ap/{invoice_id}/reject")
def ap_reject(
    request: Request, invoice_id: uuid.UUID,
    reason: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    return _ap_action(
        request, repos, principal, invoice_id,
        lambda: ap_service.reject(
            repos.session, invoice_id=invoice_id, actor=_actor(principal),
            reason=reason.strip() or None,
        ),
    )


# --------------------------------------------------------------------------- #
# Payables overview: reimburse-staff vs pay-vendors totals in one place.
# --------------------------------------------------------------------------- #
@router.get("/approvals", response_class=HTMLResponse)
def approvals_inbox(
    request: Request,
    tab: str = "expenses",
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    """The approver's front door (Appendix E1): ONE workspace, two tabs —
    staff expenses awaiting review and vendor bills awaiting approval — plus the
    classifier's 'needs a check' pages. Deep-linkable via ?tab=."""
    ids = list(principal.allowed_client_ids)
    claims = repos.claims.list_for_clients(ids, ("submitted", "in_review"))
    invoices = [
        inv for inv in ap_service.list_invoices(repos.session, ids)
        if inv.status in ("coded", "pending_approval")
    ]
    vendors = _ap_vendors_for(repos.session, invoices)
    pending_intakes = [
        r for r in intake_service.holding_queue(repos.session, ids)
        if r.routed_to == routing.QUEUE_PENDING
    ]
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "tab": ("vendor" if tab == "vendor" else "expenses"),
            "claims": claims,
            "invoices": invoices,
            "vendors": vendors,
            "pending_intakes": pending_intakes,
        },
    )


def _ap_vendors_for(session, invoices):
    from sqlalchemy import select as _select

    from ..db.models import Vendor

    vids = {inv.vendor_id for inv in invoices}
    if not vids:
        return {}
    return {v.id: v for v in session.execute(
        _select(Vendor).where(Vendor.id.in_(vids))
    ).scalars()}


@router.get("/payables", response_class=HTMLResponse)
def payables_page(
    request: Request,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    p = payables_service.payables(repos.session, principal.allowed_client_ids)
    return templates.TemplateResponse(request, "payables.html", {"p": p})


@router.post("/payables/pay")
def payables_pay(
    request: Request,
    kind: str = Form(...),
    id: str = Form(...),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Settle one payable (a staff reimbursement or a vendor bill) → paid, so it drops
    off the list. Viewers can't; the mutation is RLS-scoped + audited in its service."""
    if principal.base_role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot record payments")
    try:
        target = uuid.UUID(id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad id")
    try:
        if kind == "claim":
            _service.mark_paid(repos=repos, claim_id=target, actor=_actor(principal), principal=principal)
        elif kind == "ap":
            inv = ap_service.get_invoice(repos.session, target)
            if not principal.can_access_client(inv.client_id):
                raise HTTPException(status_code=403, detail="no grant to this client")
            ap_service.mark_paid(
                repos.session, invoice_id=target, actor=_actor(principal), payer=principal
            )
        else:
            raise HTTPException(status_code=400, detail="unknown payable kind")
        repos.session.commit()
    except (ClaimError, ap_service.ApError, SoDViolation) as exc:
        repos.session.rollback()
        set_tenant_context(repos.session, principal.firm_id, principal.allowed_client_ids)
        if isinstance(exc, (ap_service.ApNotFound, ClaimNotFound)):
            raise HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, SoDViolation):
            raise HTTPException(status_code=403, detail=str(exc))
        raise HTTPException(status_code=409, detail=str(exc))
    return RedirectResponse("/payables", status_code=303)


# --------------------------------------------------------------------------- #
# Carbon coverage (F-B): captured spend vs carbon-forwarded, per document/period.
# --------------------------------------------------------------------------- #
@router.get("/coverage", response_class=HTMLResponse)
def coverage_page(
    request: Request,
    period: str = "",
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    periods = coverage_service.coverage_report(
        repos.session, principal.allowed_client_ids, period=period or None
    )
    return templates.TemplateResponse(
        request, "coverage.html", {"periods": periods, "selected_period": period},
    )


def _ap_vendors(repos: Repos, principal: Principal):
    from sqlalchemy import select as _select
    if not principal.allowed_client_ids:
        return []
    return list(repos.session.execute(
        _select(ap_service.Vendor).where(
            ap_service.Vendor.client_id.in_(principal.allowed_client_ids)
        )
    ).scalars())


def _ap_action(request, repos, principal, invoice_id, fn):
    """Run an AP mutation and re-render the detail page on error (mirrors _action)."""
    if principal.base_role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot modify invoices")
    try:
        fn()
        repos.session.commit()
    except SoDViolation as exc:
        repos.session.rollback()
        set_tenant_context(repos.session, principal.firm_id, principal.allowed_client_ids)
        raise HTTPException(status_code=403, detail=str(exc))
    except ap_service.ApError as exc:
        repos.session.rollback()
        set_tenant_context(repos.session, principal.firm_id, principal.allowed_client_ids)
        status = 404 if isinstance(exc, ap_service.ApNotFound) else 409
        raise HTTPException(status_code=status, detail=str(exc))
    return RedirectResponse(f"/ap/{invoice_id}", status_code=303)


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_route(origin: str, destination: str, wps: list[str], route_index: int):
    """Authoritatively compute the route the claimant chose. Returns
    ``(chosen, shortest_km)``: ``shortest_km`` is the SHORTEST route's distance among
    the options (the cheapest to reimburse) — the baseline a longer chosen route is
    flagged against. (Google's ``routes[0]`` is the fastest by time, which can be
    longer than the shortest, so it is NOT the baseline.) Alternatives only exist for
    a direct trip; an out-of-range index falls back to the first route."""
    options = deps.get_directions().routes(origin, destination, wps)
    idx = route_index if 0 <= route_index < len(options) else 0
    return options[idx], min(o.distance_km for o in options)


@router.post("/capture/mileage/preview")
def web_mileage_preview(
    origin: str = Form(""),
    destination: str = Form(""),
    waypoints: str = Form("[]"),
    principal: Principal = Depends(deps.get_session_principal),
) -> JSONResponse:
    """Compute routes for the capture-page preview (km + encoded polyline) via the
    server Routes provider — so the browser never calls a deprecated Google JS
    service and the previewed distance matches what the server will reimburse. For a
    direct trip this returns alternatives (``routes[0]`` = recommended) so the
    claimant can pick the route they actually drove."""
    from ..maps import MapError

    try:
        wps = [w for w in json.loads(waypoints) if isinstance(w, str) and w.strip()]
    except json.JSONDecodeError:
        wps = []
    if not origin.strip() or not destination.strip():
        return JSONResponse({"ok": False, "error": "Enter from and to."})
    try:
        options = deps.get_directions().routes(origin.strip(), destination.strip(), wps)
    except MapError as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    return JSONResponse({
        "ok": True,
        "shortest_km": str(min(o.distance_km for o in options)),
        "routes": [
            {
                "distance_km": str(r.distance_km),
                "polyline": r.polyline,
                "stops": r.stops,
                "description": r.description,
            }
            for r in options
        ],
    })


@router.post("/capture/mileage")
def web_capture_mileage(
    request: Request,
    origin: str = Form(""),
    destination: str = Form(""),
    waypoints: str = Form("[]"),
    route_index: str = Form("0"),
    trip_date: str = Form(""),
    title: str = Form(""),
    event_id: str = Form(""),
    attested: str = Form(""),
    vehicle_id: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Create a ONE-line mileage claim. The server recomputes the distance via the
    Directions provider (authoritative for reimbursement — never trust client km),
    prices it at the per-km rate, and lands on the review screen. ``route_index``
    selects the alternative the claimant picked; the recommended distance is kept so
    a longer-than-recommended route is flagged to the approver.

    A mileage line is out-of-pocket reimbursement, so — like the main /capture form —
    the claimant must attest before it is saved (punch-list P3); the attestation is
    stamped on the claim so it clears the release gate."""
    from ..maps import MapError

    client_id = deps.default_client_id(repos.session)
    try:
        wps = [w for w in json.loads(waypoints) if isinstance(w, str) and w.strip()]
    except json.JSONDecodeError:
        wps = []
    form = {"claim_type": "travel", "event_id": event_id}
    if not origin.strip() or not destination.strip():
        return _render_capture(request, _capture_categories(repos), _events_for(repos),
                               "From and To are both required.", form)
    if not trip_date.strip():
        return _render_capture(request, _capture_categories(repos), _events_for(repos),
                               "A trip date is required for a mileage claim.", form)
    if not attested.strip():
        return _render_capture(request, _capture_categories(repos), _events_for(repos),
                               "Please confirm the out-of-pocket declaration to submit "
                               "a mileage claim.", form)
    try:
        route, shortest_km = _resolve_route(
            origin.strip(), destination.strip(), wps, _parse_int(route_index))
    except MapError as exc:
        return _render_capture(request, _capture_categories(repos), _events_for(repos),
                               f"Could not compute the route: {exc}", form)

    sd = _parse_date(trip_date)
    try:
        ev_uuid = uuid.UUID(event_id) if event_id.strip() else None
        # A dated trip is 'travel'; without a date fall back to 'general' so the
        # claim-type date rule never blocks a quick mileage claim.
        claim = _service.start_claim(
            repos=repos, firm_id=principal.firm_id, client_id=client_id,
            title=title.strip() or f"Mileage — {origin.strip()} → {destination.strip()}",
            claim_type=("travel" if sd else "general"),
            start_date=sd, end_date=sd, event_id=ev_uuid,
        )
        _service.add_mileage_line(
            repos=repos, claim=claim, origin=origin.strip(), destination=destination.strip(),
            waypoints=wps, route=route, date=trip_date or None,
            rate=deps.get_mileage_rate(), shortest_km=shortest_km,
            vehicle=vehicles_service.resolve(repos.session, client_id, vehicle_id),
        )
    except (ClaimError, ValueError) as exc:
        repos.session.rollback()
        # rollback clears the SET LOCAL RLS context; re-establish it so the
        # re-render's client-scoped queries can see rows again (else 500).
        set_tenant_context(repos.session, principal.firm_id, principal.allowed_client_ids)
        return _render_capture(request, _capture_categories(repos), _events_for(repos),
                               str(exc), form)
    _service.submit(repos=repos, claim=claim, actor=_actor(principal), line_count=1,
                    attested=True)
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
    session cookie and redirect to the inbox. On failure, re-render with a GENERIC
    error and set no cookie; repeated failures are throttled per IP + email."""
    settings = get_settings()
    limiter = request.app.state.login_limiter
    ip = client_ip(request)
    email_key = (email or "").strip().lower()
    try:
        limiter.check(ip, email_key)
    except RateLimited as rl:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Too many sign-in attempts. Please wait a few minutes and try again."},
            status_code=429,
            headers={"Retry-After": str(rl.retry_after)},
        )

    provider = DevAuthProvider(
        db, secret=settings.jwt_secret, ttl_seconds=settings.jwt_ttl_seconds,
        allow_passwordless=settings.dev_auth_allowed,
    )
    try:
        token = provider.login(email, password or None)
    except AuthError:
        limiter.record_failure(ip, email_key)
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Sign in failed — check your email and try again."},
            status_code=401,
        )
    limiter.record_success(ip, email_key)
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
CLAIMS_PAGE_SIZE = 25


def _active_ingestion_jobs(repos: Repos) -> list[IngestionJob]:
    """In-flight async captures (queued/running) for the caller's clients — shown as
    a 'processing' banner on the inbox. RLS scopes these to the request's tenant."""
    from sqlalchemy import select

    return list(
        repos.session.execute(
            select(IngestionJob)
            .where(IngestionJob.status.in_(("queued", "running")))
            .order_by(IngestionJob.created_at.desc())
        ).scalars().all()
    )


@router.get("/claims", response_class=HTMLResponse)
def claims_inbox(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    """One list serves inbox / approvals / flagged / posted — filtered by status,
    optionally searched by ``q`` (title / vendor / ref), paginated, and scoped to
    the principal's visible clients."""
    q = (q or "").strip()
    # A menu item may be a group (e.g. "attention") spanning several statuses.
    status_filter = STATUS_GROUPS.get(status, status)
    all_claims = repos.claims.list_for_clients(principal.allowed_client_ids, status_filter)
    client_names = {c.id: c.name for c in list_visible_clients(repos.session, principal)}
    summary = repos.claims.inbox_summary(principal.allowed_client_ids)
    total = summary["total"]
    summary["carbon_pct"] = (
        round(summary["carbon_count"] / total * 100) if total else 0
    )
    # Per-claim line summary for the row: a representative vendor, the line count,
    # and whether any line is carbon-relevant (the leaf) — one query, no N+1.
    # NOTE: list+search+page are applied in Python over the scoped set; for a very
    # large tenant this should move to SQL LIMIT/OFFSET + an ILIKE filter.
    lines_map = repos.claims.lines_by_claim([c.id for c in all_claims])

    if q:
        ql = q.lower()

        def _matches(c) -> bool:
            if c.title and ql in c.title.lower():
                return True
            if str(c.id).startswith(ql):
                return True
            return any(
                ln.vendor and ql in ln.vendor.lower() for ln in lines_map.get(c.id, [])
            )

        all_claims = [c for c in all_claims if _matches(c)]

    total_results = len(all_claims)
    total_pages = max(1, (total_results + CLAIMS_PAGE_SIZE - 1) // CLAIMS_PAGE_SIZE)
    page = min(max(page, 1), total_pages)
    start = (page - 1) * CLAIMS_PAGE_SIZE
    claims = all_claims[start:start + CLAIMS_PAGE_SIZE]

    row_info = {
        c.id: {
            "vendor": (lns[0].vendor if (lns := lines_map.get(c.id, [])) else None),
            "count": len(lns),
            "carbon": any(ln.carbon_relevant for ln in lns),
            "flagged": any(ln.category_id is None for ln in lns),
        }
        for c in claims
    }
    return templates.TemplateResponse(
        request,
        "claims.html",
        {
            "claims": claims,
            "row_info": row_info,
            "client_names": client_names,
            "statuses": CLAIM_STATUSES,
            "current_status": status,
            "summary": summary,
            "q": q,
            "page": page,
            "total_pages": total_pages,
            "total_results": total_results,
            "active_jobs": _active_ingestion_jobs(repos),
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
    diverted: int = 0,
) -> HTMLResponse:
    claim = repos.claims.get(claim_id)
    if claim is None:
        # Genuinely not found (or not visible under this tenant). review.html assumes
        # a populated context, so don't try to render it with a hole — return a clean
        # 404 instead of crashing on the first undefined variable.
        raise HTTPException(status_code=404, detail="claim not found")
    lines = repos.claims.lines(claim_id)
    cats = repos.categories.list_for_client(claim.client_id)
    cat_by_id = {c.id: c for c in cats}
    claimant = (
        repos.session.get(Claimant, claim.submitted_by_claimant_id)
        if claim.submitted_by_claimant_id
        else None
    )
    # Editing is only allowed while a claim is still under review — once approved
    # (or beyond) its lines are locked. Corrections to an approved claim must go
    # through send-back → resubmit, not silent amendment.
    can_edit = (
        principal.base_role != "viewer"
        and principal.can_access_client(claim.client_id)
        and claim.status in ("in_review", "submitted", "sent_back")
    )
    # After approval but before release, Finance can still edit the accounting CODING
    # (GL / cost centre / dept / project / posting date / tax) even though the expense
    # line is locked — the post-approval, pre-post coding step that makes a claim
    # postable. Mirrors the service rule in ClaimService.edit().
    can_code = (
        principal.base_role != "viewer"
        and principal.can_access_client(claim.client_id)
        and claim.status in ("approved", "partially_approved")
    )
    # Event context + budget + related claims (the late-bill / split-claim view).
    event = repos.events.get(claim.event_id) if claim.event_id else None
    budget = related = None
    if event is not None:
        spent = repos.events.spent(event.id)
        related = [c for c in repos.events.claims(event.id) if c.id != claim_id]
        budget = {
            "amount": event.budget_amount,
            "spent": spent,
            "remaining": (event.budget_amount - spent) if event.budget_amount is not None else None,
            "over": (event.budget_amount is not None and spent > event.budget_amount),
            "related_count": len(related),
        }
    carbon_count = sum(1 for ln in lines if ln.carbon_relevant)
    # Automatic duplicate detection (Appendix A): flag lines that look like an
    # expense already recorded for this client (another e-Claim, or the ERP feed).
    # Advisory only — surfaced as an approver warning, never a block.
    from ..services.duplicates import find_duplicates

    duplicate_flags = find_duplicates(repos, claim, lines)
    # Per-line OCR bounding boxes for the receipt-highlight overlay (field -> box).
    line_boxes = {str(ln.id): (ln.ocr_boxes or {}) for ln in lines}
    # Mileage lines show a route map instead of a receipt.
    line_mileage = {str(ln.id): ln.mileage for ln in lines if ln.mileage}
    # Merge/split (per-client policy): page count per multi-page line drives the
    # "N pages" badge + the Split action; the flag gates the controls (server enforces
    # it too). Only meaningful while the claim is still editable.
    line_pages = {str(ln.id): len(ln.pages) for ln in lines if ln.pages}
    allow_split = can_edit and _service._allow_document_split(repos, claim)
    maps_key = get_settings().google_maps_browser_key or get_settings().google_maps_api_key
    # Posting readiness per line (GL + resolvable cost centre) — the audit gate.
    requires_coding = _service._requires_coding(repos, claim)
    coding = {
        ln.id: {
            # Effective GL/cost centre a line posts to: its own override, else the
            # value inherited from the chosen category / claimant / event. The review
            # UI shows these so a line coded purely by its category isn't shown blank.
            "gl": _service._resolved_gl(repos, ln),
            "cost_centre": _service._resolved_cost_centre(repos, ln, claim),
            "ready": _service._posting_ready(repos, ln, claim),
        }
        for ln in lines
    }
    approved_lines = [ln for ln in lines if ln.line_status == "approved"]
    posting_ready = all(coding[ln.id]["ready"] for ln in approved_lines) if approved_lines else False
    # Approver authority limit + whether this claim is over it (already enforced).
    over_authority = (
        principal.authority_limit is not None
        and claim.total_claimed is not None
        and claim.total_claimed > principal.authority_limit
    )
    # The "verify one by one" queue across other in_review claims.
    queue = [
        c for c in repos.claims.list_for_clients(principal.allowed_client_ids, "in_review")
        if c.id != claim_id
    ]
    # When the approve controls are hidden on an in-review claim, say WHY (instead of
    # silently dropping them) — most often separation of duties: the user who keyed
    # the claim cannot approve it.
    from ..services.sod import matrix_rule_for
    matrix_rule = matrix_rule_for(repos, claim)
    can_review = can_approve(claim, principal, matrix_rule=matrix_rule) and claim.status == "in_review"
    review_block_reason = None
    if claim.status == "in_review" and not can_review:
        from ..services.sod import SoDViolation, check_can_approve

        try:
            check_can_approve(claim, principal, matrix_rule=matrix_rule)
        except SoDViolation as exc:
            review_block_reason = str(exc)
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "claim": claim,
            "lines": lines,
            "cat_by_id": cat_by_id,
            "claimant": claimant,
            "categories": cats,
            "can_code": can_code,
            "payment_methods": PAYMENT_METHODS,
            "line_boxes": line_boxes,
            "line_mileage": line_mileage,
            "line_pages": line_pages,
            "allow_split": allow_split,
            "maps_key": maps_key,
            "coding": coding,
            "requires_coding": requires_coding,
            "posting_ready": posting_ready,
            "authority_limit": principal.authority_limit,
            "over_authority": over_authority,
            "event": event,
            "budget": budget,
            "related": related,
            "carbon_count": carbon_count,
            "duplicate_flags": duplicate_flags,
            "events": repos.audit.chain("claim", claim_id),
            "can_review": can_review,
            "review_block_reason": review_block_reason,
            "can_edit": can_edit,
            "can_resubmit": can_edit and claim.status in ("submitted", "sent_back"),
            "can_release": claim.status in ("approved", "partially_approved")
            and principal.base_role != "viewer",
            # Re-attest affordance (punch-list R2): a claim that reimburses
            # out-of-pocket spend but was captured without the attestation can be
            # attested after the fact — otherwise it is permanently stuck at the
            # release gate. Only before release, only for a writer, only when there is
            # out-of-pocket spend to attest to and it isn't already attested.
            "can_attest": (
                claim.attested_by is None
                and principal.base_role != "viewer"
                and principal.can_access_client(claim.client_id)
                and claim.status not in ("released", "exported", "paid", "rejected")
                and any(ln.payment_method == "out_of_pocket" for ln in lines)
            ),
            # Reopen for amendment — only before the claim has left for another
            # system (released/exported/paid are locked).
            "can_reopen": claim.status in ("approved", "partially_approved")
            and principal.base_role != "viewer"
            and principal.can_access_client(claim.client_id),
            "next_review_id": queue[0].id if queue else None,
            "review_remaining": len(queue),
            "error": error,
            # Pages the classifier routed to the vendor-bills queue on this capture (F2).
            "diverted": diverted,
            # The user's own still-open diverted pages, offered for a one-click
            # "bring back into THIS claim" (owner request: fixing a misroute must
            # not require a trip through the vendor-bills module). Only while the
            # claim is still editable.
            "my_diverted": (
                intake_service.open_diverted_for_user(
                    repos.session, principal.allowed_client_ids, principal.user_id
                )
                if claim.status in ("submitted", "in_review", "sent_back")
                and principal.base_role != "viewer"
                else []
            ),
        },
    )


@router.get("/claims/{claim_id}/review", response_class=HTMLResponse)
def review_page(
    request: Request,
    claim_id: uuid.UUID,
    diverted: int = 0,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
) -> HTMLResponse:
    return _render_review(request, repos, principal, claim_id, diverted=diverted)


def _receipt_download_name(line) -> str:
    """A friendly filename for the receipt download: ``receipt-<line>.<ext>``.
    Sending a filename makes the same endpoint serve inline in an <img> (browsers
    ignore Content-Disposition on image subresources) yet SAVE a file when opened
    via the download link."""
    return f"receipt-{line.id}{Path(line.image_path).suffix or '.jpg'}"


@router.get("/claims/{claim_id}/image")
def claim_image(claim_id: uuid.UUID, repos: Repos = Depends(deps.get_web_repos)):
    """Serve the claim's first line image (back-compat; RLS-scoped → 404)."""
    line = repos.claims.first_line(claim_id)
    if line is None or not line.image_path or not os.path.exists(line.image_path):
        raise HTTPException(status_code=404, detail="image not available")
    return FileResponse(line.image_path, filename=_receipt_download_name(line))


@router.get("/claims/{claim_id}/lines/{line_id}/image")
def claim_line_image(
    claim_id: uuid.UUID,
    line_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
):
    """Serve one line's receipt image (RLS-scoped: invisible line → 404)."""
    line = repos.claims.line(line_id)
    if (
        line is None
        or line.claim_id != claim_id
        or not line.image_path
        or not os.path.exists(line.image_path)
    ):
        raise HTTPException(status_code=404, detail="image not available")
    return FileResponse(line.image_path, filename=_receipt_download_name(line))


def _route_markers(mil: dict) -> list[str]:
    """Static Maps endpoint pins (A=start green, B=end red) from a mileage line's
    stored origin/destination addresses."""
    markers = []
    if mil.get("origin"):
        markers.append(f"color:0x16a34a|label:A|{mil['origin']}")
    if mil.get("destination"):
        markers.append(f"color:0xdc2626|label:B|{mil['destination']}")
    return markers


@router.get("/claims/{claim_id}/lines/{line_id}/route.png")
def claim_line_route(
    claim_id: uuid.UUID,
    line_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Render a mileage line's route as a Google Static Map, fetched SERVER-SIDE
    with the server key (so no browser key is needed and the key stays hidden).
    RLS-scoped; a non-mileage/invisible line or an unconfigured key → 404 (the
    viewer then shows its 'route map unavailable' fallback)."""
    from ..maps import MapError, fetch_static_map

    line = repos.claims.line(line_id)
    if line is None or line.claim_id != claim_id or not line.mileage:
        raise HTTPException(status_code=404, detail="no route for this line")
    mil = line.mileage
    try:
        png = fetch_static_map(
            get_settings().google_maps_api_key,
            polyline=mil.get("polyline"),
            markers=_route_markers(mil),
        )
    except MapError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(content=png, media_type="image/png")


@router.get("/mileage/route.png")
def mileage_route_preview(
    polyline: str = "",
    origin: str = "",
    destination: str = "",
    principal: Principal = Depends(deps.get_session_principal),
):
    """Server-proxied Static Map for the capture / add-mileage PREVIEW (before a
    line exists). Takes the encoded polyline from the route-preview call (and/or the
    endpoint addresses for the pins). Same server-key proxy as the per-line route."""
    from ..maps import MapError, fetch_static_map

    markers = []
    if origin:
        markers.append(f"color:0x16a34a|label:A|{origin}")
    if destination:
        markers.append(f"color:0xdc2626|label:B|{destination}")
    try:
        png = fetch_static_map(
            get_settings().google_maps_api_key,
            polyline=(polyline or None),
            markers=markers or None,
        )
    except MapError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(content=png, media_type="image/png")


# --------------------------------------------------------------------------- #
# Review actions (thin wrappers over ClaimService; the service is the gate)
# --------------------------------------------------------------------------- #
def _action(request, repos, principal, claim_id, fn) -> HTMLResponse | RedirectResponse:
    try:
        fn()
    except ClaimError as exc:
        # The tenant context is set with SET LOCAL (transaction-scoped), so the
        # rollback that clears the failed action ALSO clears the RLS GUCs. Re-apply
        # it before re-rendering, otherwise the review re-fetch runs context-less,
        # RLS hides the claim, and the error page itself 500s.
        repos.session.rollback()
        set_tenant_context(repos.session, principal.firm_id, principal.allowed_client_ids)
        return _render_review(request, repos, principal, claim_id, error=str(exc))
    return RedirectResponse(f"/claims/{claim_id}/review", status_code=303)


@router.post("/claims/{claim_id}/edit")
def web_edit(
    request: Request,
    claim_id: uuid.UUID,
    line_id: str = Form(""),
    vendor: str = Form(""),
    doc_no: str = Form(""),
    doc_date: str = Form(""),
    currency: str = Form(""),
    total_amount: str = Form(""),
    expense_type: str = Form(""),
    quantity: str = Form(""),
    unit: str = Form(""),
    business_reason: str = Form(""),
    payment_method: str = Form(""),
    # Accounting coding
    gl_code: str = Form(""),
    cost_centre_override: str = Form(""),
    department: str = Form(""),
    project_code: str = Form(""),
    posting_date: str = Form(""),
    supplier_tax_id: str = Form(""),
    tax_amount: str = Form(""),
    tax_code: str = Form(""),
    tax_inclusive: str = Form(""),
    fx_rate: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    fields: dict = {}
    for key, value in (
        ("vendor", vendor), ("doc_no", doc_no), ("doc_date", doc_date),
        ("currency", currency), ("expense_type", expense_type), ("unit", unit),
        ("business_reason", business_reason), ("payment_method", payment_method),
        ("gl_code", gl_code), ("cost_centre_override", cost_centre_override),
        ("department", department), ("project_code", project_code),
        ("supplier_tax_id", supplier_tax_id), ("tax_code", tax_code),
    ):
        if value != "":
            fields[key] = value
    # total_amount stays non-clearable (a line always has its printed gross); the
    # OPTIONAL numerics are clearable — the verify form always renders these inputs
    # prefilled, so a reviewer BLANKING one is an explicit "remove this value"
    # (an OCR-hallucinated quantity was previously overtypeable but never
    # removable, F-E item 6). unit clears alongside quantity semantics.
    if total_amount != "":
        fields["total_amount"] = Decimal(total_amount)
    for key, value in (("quantity", quantity), ("tax_amount", tax_amount),
                       ("fx_rate", fx_rate)):
        fields[key] = Decimal(value) if value != "" else None
    if unit == "":
        fields["unit"] = None
    if posting_date != "":
        fields["posting_date"] = _parse_date(posting_date)
    if tax_inclusive != "":
        fields["tax_inclusive"] = tax_inclusive in ("1", "true", "on", "yes")
    lid = uuid.UUID(line_id) if line_id.strip() else None
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.edit(
            repos=repos, claim_id=claim_id, fields=fields, line_id=lid,
            actor=_actor(principal), principal=principal,
        ),
    )


@router.post("/claims/{claim_id}/lines/merge")
def web_merge_lines(
    request: Request,
    claim_id: uuid.UUID,
    line_ids: list[str] = Form(default=[]),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
    image_dir: Path = Depends(deps.get_image_dir),
):
    """Merge the selected lines into one (pages of one invoice). Flag-gated + audited
    in the service; the UI only offers it when allow_document_split is on."""
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.merge_lines(
            repos=repos, claim_id=claim_id, line_ids=line_ids,
            actor=_actor(principal), image_dir=image_dir, principal=principal,
        ),
    )


@router.post("/claims/{claim_id}/lines/split")
def web_split_line(
    request: Request,
    claim_id: uuid.UUID,
    line_id: str = Form(...),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Split a multi-page line back into one line per page. Flag-gated + audited."""
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.split_line(
            repos=repos, claim_id=claim_id, line_id=uuid.UUID(line_id),
            actor=_actor(principal), principal=principal,
        ),
    )


@router.post("/claims/{claim_id}/header")
def web_edit_header(
    request: Request,
    claim_id: uuid.UUID,
    title: str = Form(""),
    purpose: str = Form(""),
    remarks: str = Form(""),
    posting_date: str = Form(""),
    department: str = Form(""),
    project_code: str = Form(""),
    claim_currency: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Edit the claim's document-header (grouping) fields from the review screen.
    Text fields are sent as-is (empty string clears them); ``posting_date`` is
    parsed to a date. The service gates on claim status + tenant access."""
    fields: dict = {
        "title": title, "purpose": purpose, "remarks": remarks,
        "department": department, "project_code": project_code,
        "claim_currency": claim_currency,
        "posting_date": _parse_date(posting_date) if posting_date != "" else None,
    }
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.edit_header(
            repos=repos, claim_id=claim_id, fields=fields, actor=_actor(principal),
            principal=principal,
        ),
    )


@router.post("/claims/{claim_id}/mileage")
def web_add_mileage(
    request: Request,
    claim_id: uuid.UUID,
    origin: str = Form(""),
    destination: str = Form(""),
    waypoints: str = Form("[]"),
    route_index: str = Form("0"),
    trip_date: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Add a mileage line to an existing (editable) claim from the review screen —
    so a claim can mix receipts + mileage, added before, after, or while reviewing.
    The server recomputes the authoritative distance and prices it; ``route_index``
    selects the claimant's chosen alternative."""
    from ..maps import MapError

    try:
        wps = [w for w in json.loads(waypoints) if isinstance(w, str) and w.strip()]
    except json.JSONDecodeError:
        wps = []
    if not origin.strip() or not destination.strip():
        return _render_review(request, repos, principal, claim_id,
                              error="Mileage needs a From and a To.")
    if not trip_date.strip():
        return _render_review(request, repos, principal, claim_id,
                              error="A mileage line needs a trip date.")
    try:
        route, shortest_km = _resolve_route(
            origin.strip(), destination.strip(), wps, _parse_int(route_index))
    except MapError as exc:
        return _render_review(request, repos, principal, claim_id,
                              error=f"Could not compute the route: {exc}")
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.add_mileage_to_claim(
            repos=repos, claim_id=claim_id, origin=origin.strip(),
            destination=destination.strip(), waypoints=wps, route=route,
            date=trip_date or None, rate=deps.get_mileage_rate(), actor=_actor(principal),
            principal=principal, shortest_km=shortest_km,
        ),
    )


@router.post("/claims/{claim_id}/category")
def web_assign_category(
    request: Request,
    claim_id: uuid.UUID,
    category_id: str = Form(...),
    line_id: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    lid = uuid.UUID(line_id) if line_id.strip() else None
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.edit(
            repos=repos, claim_id=claim_id, fields={}, line_id=lid,
            actor=_actor(principal), principal=principal,
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


@router.post("/claims/{claim_id}/decide")
async def web_decide(
    request: Request,
    claim_id: uuid.UUID,
    note: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Partial approval — one action, per-line outcomes. The review form posts a
    ``line_<id>`` = approved|queried|rejected and an optional ``reason_<id>`` per
    line; we assemble them into a single :meth:`ClaimService.decide`."""
    form = await request.form()
    decisions: dict = {}
    for key, value in form.items():
        if key.startswith("line_") and value:
            try:
                lid = uuid.UUID(key[len("line_"):])
            except ValueError:
                continue
            reason = (form.get(f"reason_{lid}") or "").strip() or None
            decisions[lid] = (value, reason)
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.decide(
            repos=repos, claim_id=claim_id, reviewer=principal,
            decisions=decisions, actor=_actor(principal), note=note.strip() or None,
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


@router.post("/claims/{claim_id}/unapprove")
def web_unapprove(
    request: Request,
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.unapprove(
            repos=repos, claim_id=claim_id, reviewer=principal, actor=_actor(principal)
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
        lambda: _service.resubmit(
            repos=repos, claim_id=claim_id, actor=_actor(principal), principal=principal
        ),
    )


@router.post("/claims/{claim_id}/attest")
def web_attest(
    request: Request,
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Record the claimant's out-of-pocket attestation after the fact, so a claim that
    was captured without it (pre-P3 rows / API upload) can clear the release gate
    (punch-list R2) instead of being permanently stuck."""
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.attest(
            repos=repos, claim_id=claim_id, actor=_actor(principal), principal=principal
        ),
    )


@router.post("/claims/{claim_id}/release")
def web_release(
    request: Request,
    claim_id: uuid.UUID,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    # Release is a downstream sign-off (forwards to CarbonNext/ERP): attribute it
    # to the real user and gate on role, not the anonymous "system" actor.
    return _action(
        request, repos, principal, claim_id,
        lambda: _service.release(
            repos=repos, claim_id=claim_id, actor=_actor(principal), principal=principal
        ),
    )


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
@router.get("/ledger", response_class=HTMLResponse)
def ledger_page(request: Request, repos: Repos = Depends(deps.get_web_repos)) -> HTMLResponse:
    client_id = deps.default_client_id(repos.session)
    ledger_repo = LedgerRepository(repos.session)
    entries = ledger_repo.entries(client_id)
    counts = ledger_repo.direction_counts(client_id)
    forwarded, reversed_ = counts.get("forward", 0), counts.get("reversal", 0)
    return templates.TemplateResponse(
        request,
        "ledger.html",
        {
            "entries": entries,
            "forwarded": forwarded,
            "reversed": reversed_,
            "total": forwarded + reversed_,
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
    edit: str = "",
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
) -> HTMLResponse:
    eid = _opt_uuid(edit)
    editing = repos.categories.get_by_id(eid) if eid else None
    return _render_categories(request, repos, principal, editing=editing)


@router.post("/admin/categories")
def admin_save_category(
    request: Request,
    category_id: str = Form(""),
    client_id: str = Form(...),
    name: str = Form(...),
    expense_type: str = Form(...),
    carbon_relevant: str = Form(""),
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
    # Checkbox: present (any value) => carbon-relevant (forwarded to CarbonNext).
    relevant = bool(carbon_relevant)
    try:
        with repos.session.begin_nested():
            if category_id.strip():
                cat = repos.categories.get_by_id(uuid.UUID(category_id))
                if cat is None or cat.client_id != cid:
                    raise LookupError
                cat.name, cat.expense_type = name, expense_type
                cat.gl_export_code = gl_export_code or None
                cat.carbon_relevant = relevant
                cat.default_limit, cat.status = limit, status or "active"
            else:
                repos.session.add(
                    Category(
                        firm_id=principal.firm_id, client_id=cid, name=name,
                        expense_type=expense_type, carbon_relevant=relevant,
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
            error="A category with that name already exists for this client.",
        )
    return RedirectResponse("/admin/categories", status_code=303)


def _render_rates(request, repos, principal, *, error=None) -> HTMLResponse:
    clients = list_visible_clients(repos.session, principal)
    return templates.TemplateResponse(
        request,
        "admin_rates.html",
        {
            "rates": fx_service.list_rates(repos.session, [c.id for c in clients]),
            "clients": clients,
            "client_names": {c.id: c.name for c in clients},
            "error": error,
        },
    )


@router.get("/admin/rates", response_class=HTMLResponse)
def admin_rates(
    request: Request,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
) -> HTMLResponse:
    return _render_rates(request, repos, principal)


@router.post("/admin/rates")
def admin_save_rate(
    request: Request,
    client_id: str = Form(...),
    currency: str = Form(...),
    period: str = Form(...),          # <input type=month> → "YYYY-MM"
    rate_to_myr: str = Form(...),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
):
    import datetime as _dt

    try:
        cid = uuid.UUID(client_id)
        month = _dt.date.fromisoformat(period.strip() + "-01")
        rate = Decimal(rate_to_myr.strip())
    except (ValueError, InvalidOperation):
        return _render_rates(request, repos, principal, error="Invalid month or rate.")
    if cid not in principal.allowed_client_ids:
        return _render_rates(request, repos, principal, error="You cannot manage that client.")
    ccy = currency.strip().upper()
    if len(ccy) != 3 or not ccy.isalpha() or ccy == "MYR":
        return _render_rates(request, repos, principal,
                             error="Currency must be a 3-letter ISO code (not MYR).")
    if rate <= 0:
        return _render_rates(request, repos, principal, error="The rate must be positive.")
    fx_service.upsert_rate(
        repos.session, firm_id=principal.firm_id, client_id=cid,
        currency=ccy, period=month, rate_to_myr=rate, actor=_actor(principal),
    )
    return RedirectResponse("/admin/rates", status_code=303)


@router.post("/admin/rates/delete")
def admin_delete_rate(
    request: Request,
    rate_id: str = Form(...),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
):
    rid = _opt_uuid(rate_id)
    if rid is not None:
        fx_service.delete_rate(repos.session, rate_id=rid, actor=_actor(principal))
    return RedirectResponse("/admin/rates", status_code=303)


def _render_vehicles(request, repos, principal, *, editing=None, error=None) -> HTMLResponse:
    from ..db.models import VEHICLE_TYPES

    clients = list_visible_clients(repos.session, principal)
    claimants = repos.claimants.list_for_clients([c.id for c in clients])
    return templates.TemplateResponse(
        request,
        "admin_vehicles.html",
        {
            "vehicles": vehicles_service.list_for_clients(repos.session, [c.id for c in clients]),
            "vehicle_types": VEHICLE_TYPES,
            "clients": clients,
            "client_names": {c.id: c.name for c in clients},
            "claimants": claimants,
            "claimant_names": {cm.id: cm.name for cm in claimants},
            "editing": editing,
            "error": error,
        },
    )


@router.get("/admin/vehicles", response_class=HTMLResponse)
def admin_vehicles(
    request: Request,
    edit: str = "",
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
) -> HTMLResponse:
    from ..db.models import Vehicle

    eid = _opt_uuid(edit)
    editing = repos.session.get(Vehicle, eid) if eid else None
    return _render_vehicles(request, repos, principal, editing=editing)


@router.post("/admin/vehicles")
def admin_save_vehicle(
    request: Request,
    vehicle_id: str = Form(""),
    client_id: str = Form(...),
    label: str = Form(...),
    vehicle_type: str = Form(...),
    engine_size: str = Form(""),
    usual_claimant_id: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
):
    from ..db.models import VEHICLE_TYPES, Vehicle

    try:
        cid = uuid.UUID(client_id)
    except ValueError:
        return _render_vehicles(request, repos, principal, error="Invalid client.")
    if cid not in principal.allowed_client_ids:
        return _render_vehicles(request, repos, principal, error="You cannot manage that client.")
    if vehicle_type not in VEHICLE_TYPES:
        return _render_vehicles(request, repos, principal, error="Unknown vehicle type.")
    usual = _opt_uuid(usual_claimant_id)
    try:
        with repos.session.begin_nested():
            if vehicle_id.strip():
                v = repos.session.get(Vehicle, uuid.UUID(vehicle_id))
                if v is None or v.client_id != cid:
                    raise LookupError
                v.label, v.vehicle_type = label.strip(), vehicle_type
                v.engine_size = engine_size.strip() or None
                v.usual_claimant_id = usual
            else:
                repos.session.add(Vehicle(
                    firm_id=principal.firm_id, client_id=cid, label=label.strip(),
                    vehicle_type=vehicle_type, engine_size=engine_size.strip() or None,
                    usual_claimant_id=usual,
                ))
            repos.session.flush()
    except LookupError:
        return _render_vehicles(request, repos, principal, error="Vehicle not found.")
    except IntegrityError:
        return _render_vehicles(
            request, repos, principal,
            error="A vehicle with that label already exists for this client.",
        )
    return RedirectResponse("/admin/vehicles", status_code=303)


@router.post("/admin/vehicles/toggle")
def admin_toggle_vehicle(
    request: Request,
    vehicle_id: str = Form(...),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
):
    from ..db.models import Vehicle

    vid = _opt_uuid(vehicle_id)
    v = repos.session.get(Vehicle, vid) if vid else None
    if v is not None:
        v.active = not v.active
        repos.session.flush()
    return RedirectResponse("/admin/vehicles", status_code=303)


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
    edit: str = "",
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
) -> HTMLResponse:
    eid = _opt_uuid(edit)
    editing = repos.claimants.get_by_id(eid) if eid else None
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
    position: str = Form(""),
    department: str = Form(""),
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
                cm.position, cm.department = position or None, department or None
                cm.status = status or "active"
            else:
                repos.session.add(
                    Claimant(
                        firm_id=principal.firm_id, client_id=cid, name=name,
                        phone=phone or None, email=email or None,
                        employee_ref=employee_ref or None, cost_centre=cost_centre or None,
                        position=position or None, department=department or None,
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


# --------------------------------------------------------------------------- #
# Admin: events + budgets (the trip/training grouping a claim can attach to)
# --------------------------------------------------------------------------- #
def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value.strip() else None
    except ValueError:
        return None


def _render_events(request, repos, principal, *, editing=None, error=None) -> HTMLResponse:
    clients = list_visible_clients(repos.session, principal)
    events = repos.events.list_for_clients([c.id for c in clients])
    # Budget rollup per event: spent / remaining for the list view.
    rollup = {}
    for ev in events:
        spent = repos.events.spent(ev.id)
        rollup[ev.id] = {
            "spent": spent,
            "remaining": (ev.budget_amount - spent) if ev.budget_amount is not None else None,
            "over": (ev.budget_amount is not None and spent > ev.budget_amount),
        }
    return templates.TemplateResponse(
        request,
        "admin_events.html",
        {
            "events": events,
            "rollup": rollup,
            "clients": clients,
            "client_names": {c.id: c.name for c in clients},
            "event_types": EVENT_TYPES,
            "editing": editing,
            "error": error,
        },
    )


@router.get("/admin/events", response_class=HTMLResponse)
def admin_events(
    request: Request,
    edit: str = "",
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
) -> HTMLResponse:
    eid = _opt_uuid(edit)
    editing = repos.events.get(eid) if eid else None
    return _render_events(request, repos, principal, editing=editing)


@router.post("/admin/events")
def admin_save_event(
    request: Request,
    event_id: str = Form(""),
    client_id: str = Form(...),
    title: str = Form(...),
    purpose: str = Form(""),
    event_type: str = Form("other"),
    attendee_count: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    location: str = Form(""),
    department: str = Form(""),
    cost_centre: str = Form(""),
    project_code: str = Form(""),
    budget_amount: str = Form(""),
    budget_currency: str = Form("MYR"),
    status: str = Form("active"),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
):
    try:
        cid = uuid.UUID(client_id)
        budget = Decimal(budget_amount) if budget_amount.strip() else None
        pax = int(attendee_count) if attendee_count.strip() else None
    except (ValueError, InvalidOperation):
        return _render_events(request, repos, principal, error="Invalid client, budget or pax.")
    if cid not in principal.allowed_client_ids:
        return _render_events(request, repos, principal, error="You cannot manage that client.")
    fields = dict(
        title=title, purpose=purpose or None, event_type=event_type or None,
        attendee_count=pax, start_date=_parse_date(start_date), end_date=_parse_date(end_date),
        location=location or None, department=department or None,
        cost_centre=cost_centre or None, project_code=project_code or None,
        budget_amount=budget, budget_currency=budget_currency or "MYR",
        status=status or "active",
    )
    try:
        with repos.session.begin_nested():
            if event_id.strip():
                ev = repos.events.get(uuid.UUID(event_id))
                if ev is None or ev.client_id != cid:
                    raise LookupError
                for k, v in fields.items():
                    setattr(ev, k, v)
            else:
                repos.events.add(Event(firm_id=principal.firm_id, client_id=cid, **fields))
            repos.session.flush()
    except LookupError:
        return _render_events(request, repos, principal, error="Event not found.")
    return RedirectResponse("/admin/events", status_code=303)


# --------------------------------------------------------------------------- #
# Admin: approval authority matrix (Appendix B, Phase-1 — single-tier)
# --------------------------------------------------------------------------- #
# Starter row-sets the wizard/template picker writes. Bands are non-overlapping to
# the cent (numeric(14,2)): each next band's floor is the previous ceiling + 0.01,
# so exactly one rule ever governs an amount. Amounts are editable defaults (MYR).
#
# Every template seeds exactly ONE approval per band (``approvals_required = 1``).
# Multi-approval counting ("two partners above 10k") needs the partial-approval
# state machine that arrives in Phase-2; until the engine actually *enforces* a
# count, seeding ``> 1`` would be a promised-but-unenforced control (punch-list
# P1), which is worse than none. Enterprise adds per-department + multi-step in
# Phase-2 (same engine, more rows); at launch it seeds the Growing set.
APPROVAL_TEMPLATES = {
    "starter": {
        "label": "Starter", "profile": "Micro team, under 20 staff",
        "summary": "Any amount → a manager (one approval).",
        "rules": [(None, None, "manager", 1)],
    },
    "small": {
        "label": "Small business", "profile": "20–100 staff",
        "summary": "≤ 2,000 → manager; above → partner.",
        "rules": [(None, "2000", "manager", 1), ("2000.01", None, "partner", 1)],
    },
    "growing": {
        "label": "Growing", "profile": "100–500 staff",
        "summary": "≤ 1,000 → manager; 1,000–10,000 → partner; above → a partner.",
        "rules": [
            (None, "1000", "manager", 1),
            ("1000.01", "10000", "partner", 1),
            ("10000.01", None, "partner", 1),
        ],
    },
    "enterprise": {
        "label": "Enterprise", "profile": "500+ staff",
        "summary": "Growing tiers now; per-department & multi-step approval in Phase-2.",
        "rules": [
            (None, "1000", "manager", 1),
            ("1000.01", "10000", "partner", 1),
            ("10000.01", None, "partner", 1),
        ],
    },
}
APPROVER_ROLES = ["manager", "partner", "approver"]

# Phase-1 enforces a single approval per band. The engine does not yet count
# multiple sign-offs, so we clamp every write path to 1 rather than let the UI or
# a crafted POST persist an ``approvals_required > 1`` control that nothing
# enforces (punch-list P1). Lift this when the partial-approval state machine lands.
PHASE1_APPROVALS_REQUIRED = 1


def _render_approvals(request, repos, principal, *, client_id=None, error=None) -> HTMLResponse:
    clients = list_visible_clients(repos.session, principal)
    selected = client_id if (client_id and any(c.id == client_id for c in clients)) else (
        clients[0].id if clients else None
    )
    rules = repos.approvals.list_for_clients([selected]) if selected else []
    return templates.TemplateResponse(
        request,
        "admin_approvals.html",
        {
            "clients": clients,
            "client_names": {c.id: c.name for c in clients},
            "selected_client_id": selected,
            "rules": rules,
            "templates": APPROVAL_TEMPLATES,
            "roles": APPROVER_ROLES,
            "error": error,
        },
    )


def _visible_client(repos, principal, client_id: uuid.UUID) -> bool:
    return any(c.id == client_id for c in list_visible_clients(repos.session, principal))


@router.get("/admin/approvals", response_class=HTMLResponse)
def admin_approvals(
    request: Request,
    client_id: str = "",
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
) -> HTMLResponse:
    return _render_approvals(request, repos, principal, client_id=_opt_uuid(client_id))


@router.post("/admin/approvals/template")
def admin_apply_template(
    request: Request,
    client_id: str = Form(...),
    template: str = Form(...),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
):
    """Replace a client's matrix with a named starter template's editable rows."""
    try:
        cid = uuid.UUID(client_id)
    except ValueError:
        return _render_approvals(request, repos, principal, error="Invalid client.")
    if template not in APPROVAL_TEMPLATES or not _visible_client(repos, principal, cid):
        return _render_approvals(request, repos, principal, client_id=cid,
                                 error="Unknown template or client.")
    repos.approvals.delete_for_client(cid)
    for mn, mx, role, _req in APPROVAL_TEMPLATES[template]["rules"]:
        repos.approvals.add(ApprovalMatrixRule(
            firm_id=principal.firm_id, client_id=cid,
            min_amount=Decimal(mn) if mn else None,
            max_amount=Decimal(mx) if mx else None,
            step_order=1, approver_role=role,
            # This launch UI configures the e-Claim matrix; scope it explicitly so it
            # doesn't silently also govern AP invoice approvals (F7).
            scope_module="eclaim",
            approvals_required=PHASE1_APPROVALS_REQUIRED, active=True,
        ))
    return RedirectResponse(f"/admin/approvals?client_id={cid}", status_code=303)


@router.post("/admin/approvals/add")
def admin_add_rule(
    request: Request,
    client_id: str = Form(...),
    min_amount: str = Form(""),
    max_amount: str = Form(""),
    approver_role: str = Form(""),
    scope_module: str = Form("eclaim"),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
):
    """Add a single-tier (step 1) rule to a client's matrix.

    ``approvals_required`` is fixed at :data:`PHASE1_APPROVALS_REQUIRED` (1) and not
    taken from the form: the engine enforces a single sign-off per band in Phase-1,
    so accepting a caller-supplied count would let a crafted POST persist an
    unenforced multi-approval control (punch-list P1).

    ``scope_module`` picks WHICH approvals the band governs — staff expenses
    (``eclaim``) or vendor bills (``ap``). Anything else (including a crafted blank,
    which would mean "all modules") clamps to ``eclaim``; the NULL=all state stays a
    deliberate DB-only escape hatch, never creatable from the UI (F7)."""
    try:
        cid = uuid.UUID(client_id)
        mn = Decimal(min_amount) if min_amount.strip() else None
        mx = Decimal(max_amount) if max_amount.strip() else None
    except (ValueError, InvalidOperation):
        return _render_approvals(request, repos, principal, error="Invalid amount or count.")
    if not _visible_client(repos, principal, cid):
        return _render_approvals(request, repos, principal, error="You cannot manage that client.")
    if mn is not None and mx is not None and mx < mn:
        return _render_approvals(request, repos, principal, client_id=cid,
                                 error="The band's maximum is below its minimum.")
    role = approver_role if approver_role in APPROVER_ROLES else None
    module = scope_module if scope_module in ("eclaim", "ap") else "eclaim"
    repos.approvals.add(ApprovalMatrixRule(
        firm_id=principal.firm_id, client_id=cid, min_amount=mn, max_amount=mx,
        step_order=1, approver_role=role, scope_module=module,
        approvals_required=PHASE1_APPROVALS_REQUIRED, active=True,
    ))
    return RedirectResponse(f"/admin/approvals?client_id={cid}", status_code=303)


@router.post("/admin/approvals/delete")
def admin_delete_rule(
    request: Request,
    rule_id: str = Form(...),
    client_id: str = Form(""),
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
):
    try:
        rid = uuid.UUID(rule_id)
    except ValueError:
        return _render_approvals(request, repos, principal, error="Invalid rule.")
    rule = repos.approvals.get(rid)
    if rule is not None and _visible_client(repos, principal, rule.client_id):
        cid = rule.client_id
        repos.session.delete(rule)
        repos.session.flush()
        return RedirectResponse(f"/admin/approvals?client_id={cid}", status_code=303)
    return _render_approvals(request, repos, principal, error="Rule not found.")
