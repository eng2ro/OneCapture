"""Web pages: Capture, Claims inbox, Review, Ledger. Server-rendered views over
the same services as the JSON API.

The inbox and review pages read through the repositories (RLS-scoped via the
request principal); the review actions POST to thin handlers here that call
:class:`ClaimService` and redirect back. The service stays the real gate — the
SoD/authority guard runs on approve/send-back/reject regardless of which buttons
the page chose to draw.
"""

from __future__ import annotations

import json
import os
import uuid
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
)
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..api import deps
from ..auth.principal import Principal, list_visible_clients
from ..auth.provider import AuthError, DevAuthProvider
from ..config import get_settings
from ..db.models import Category, Claimant, Event
from ..ocr.base import Extraction, ExpenseType, OcrError, OcrProvider, Unit
from ..repositories import ClaimRepository, LedgerRepository
from ..services.claims import CLAIM_TYPES, ClaimError, ClaimService, Repos
from ..services.sod import can_approve
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
    ctx: dict = {
        "principal": principal,
        "is_firm_scoped": bool(principal and principal.is_firm_scoped),
        "nav_counts": {},
        "nav_total": 0,
        "scope_name": None,
    }
    db = getattr(request.state, "db", None)
    if principal is None or db is None:
        return ctx
    try:
        set_tenant_context(db, principal.firm_id, principal.allowed_client_ids)
        counts = ClaimRepository(db).status_counts(principal.allowed_client_ids)
        ctx["scope_name"] = _scope_name(list_visible_clients(db, principal))
    except Exception:  # nav chrome must never break a page render
        return ctx
    ctx["nav_counts"] = counts
    ctx["nav_total"] = sum(counts.values())
    return ctx


templates = Jinja2Templates(
    directory=str(WEB_DIR / "templates"), context_processors=[_nav_context]
)

router = APIRouter(tags=["web"])
_service = ClaimService()

CLAIM_STATUSES = [
    "submitted", "in_review", "approved", "partially_approved",
    "sent_back", "released", "rejected", "exported", "paid",
]
PAYMENT_METHODS = ["out_of_pocket", "corporate_card", "company_paid"]
EVENT_TYPES = ["training", "travel", "client_meeting", "conference", "team", "project", "other"]
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


def _create_inline_event(
    repos: Repos, principal: Principal, client_id: uuid.UUID, *,
    title: str, start: str, end: str,
) -> uuid.UUID:
    """Mint a lightweight trip from the Submit page so a staffer who is first to
    file for a trip isn't blocked by 'no event exists yet'. Title + date range only
    — the budget and cost centre stay the manager's to fill later under Manage →
    Events. Returns the new event id."""
    title = title.strip()
    sd, ed = _parse_date(start), _parse_date(end)
    if not title:
        raise ClaimError("a new trip needs a title")
    if sd is None or ed is None:
        raise ClaimError("a new trip needs a start and end date")
    if ed < sd:
        raise ClaimError("the end date is before the start date")
    ev = repos.events.add(
        Event(
            firm_id=principal.firm_id, client_id=client_id, title=title,
            event_type="travel", start_date=sd, end_date=ed,
            organiser_user_id=principal.user_id, status="active",
        )
    )
    return ev.id


def _render_capture(
    request: Request,
    categories: list[Category],
    events: list | None = None,
    error: str | None = None,
    form: dict | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
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
            "error": error,
            # Echo the header fields back on a validation error so the user does
            # not have to re-pick the type/dates (the receipts are re-dropped).
            "form": form or {},
        },
    )


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
        extraction = ocr.extract(image_bytes, media_type)
    except OcrError as exc:
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


def _item_has_data(item) -> bool:
    """True only if a per-file ``items`` entry actually carries read data. A file
    the page hadn't finished reading when the batch was submitted serializes as a
    field-less/all-null entry — we must NOT treat that as 'verified empty' (that
    was the bug: such receipts were saved blank instead of being read). When this
    is False the server OCRs the image itself."""
    if not isinstance(item, dict):
        return False
    if item.get("category_id") or (item.get("expense_type") or "other") != "other":
        return True
    return any(item.get(k) for k in ("vendor", "doc_no", "date", "total_amount", "quantity", "unit"))


def _extraction_from_item(item: dict) -> Extraction:
    """Build an Extraction from one per-file ``items`` entry (the verified /
    auto-captured fields the capture page already read client-side)."""
    return Extraction(
        vendor=item.get("vendor") or None,
        doc_no=item.get("doc_no") or None,
        date=item.get("date") or None,
        total_amount=Decimal(item["total_amount"]) if item.get("total_amount") else None,
        expense_type=item.get("expense_type") or "other",
        quantity=Decimal(item["quantity"]) if item.get("quantity") else None,
        unit=item.get("unit") or None,
        boxes=item.get("boxes") or None,   # OCR field positions read client-side
    )


# Sentinel the Submit page posts in ``event_id`` to mean "create a new trip from
# the inline fields" rather than attach an existing event.
NEW_EVENT = "__new__"


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
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
    image_dir: Path = Depends(deps.get_image_dir),
    ocr: OcrProvider = Depends(deps.get_ocr),
):
    """Capture one claim from a batch of receipts: ONE ``in_review`` claim whose
    LINES are the dropped receipts (auto-captured, category suggested where
    unambiguous, unmapped otherwise). Fields the page read client-side arrive in
    ``items`` (aligned to ``files`` by order); a file with no item is OCR'd
    server-side. A bad receipt is skipped (its own savepoint), the rest still
    land. The whole claim then goes to its review screen for line-by-line verify.

    The claim header carries a compulsory ``claim_type`` and, for a non-general
    standalone claim, a date range — validated in :meth:`ClaimService.start_claim`.
    A staffer can also create a trip inline (``event_id == '__new__'``): we mint the
    Event (title + dates only; the manager fills budget later) and attach to it."""
    from ..maps import MapError

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
    # Echo the header fields back if validation fails (receipts get re-dropped).
    form = {
        "title": title, "claim_type": claim_type, "purpose": purpose,
        "remarks": remarks, "posting_date": posting_date,
        "start_date": start_date, "end_date": end_date, "event_id": event_id,
    }

    try:
        if event_id == NEW_EVENT:
            ev_uuid = _create_inline_event(
                repos, principal, client_id,
                title=new_event_title, start=new_event_start, end=new_event_end,
            )
            # The claim inherits the trip's dates; type defaults to travel when the
            # staffer didn't pick a more specific one for a brand-new trip.
            sd, ed = _parse_date(new_event_start), _parse_date(new_event_end)
            # A brand-new trip is never 'general'; honour a specific pick, else travel.
            ctype = claim_type if claim_type in CLAIM_TYPES and claim_type != "general" else "travel"
        else:
            ev_uuid = uuid.UUID(event_id) if event_id.strip() else None
            sd, ed = _parse_date(start_date), _parse_date(end_date)
            ctype = claim_type
        claim = _service.start_claim(
            repos=repos,
            firm_id=principal.firm_id,
            client_id=client_id,
            title=title.strip() or None,
            purpose=purpose.strip() or None,
            remarks=remarks.strip() or None,
            posting_date=_parse_date(posting_date) if posting_date.strip() else None,
            claim_type=ctype,
            start_date=sd,
            end_date=ed,
            event_id=ev_uuid,
            # Record the keying firm user so SoD blocks them approving it.
            created_by_user_id=principal.user_id,
        )
    except (ClaimError, ValueError) as exc:
        repos.session.rollback()
        return _render_capture(
            request, _capture_categories(repos), _events_for(repos), str(exc), form
        )
    added = 0
    errors: list[str] = []
    for i, f in enumerate(files):
        # The file input always posts; an empty selection arrives as a part with no
        # filename — skip it so a mileage-only claim isn't flagged as a bad receipt.
        if not (f.filename or "").strip():
            continue
        name = f.filename or f"receipt {i + 1}"
        media_type = f.content_type or "application/octet-stream"
        if media_type not in SUPPORTED_MEDIA:
            errors.append(f"{name}: unsupported file type")
            continue
        image_bytes = await f.read()
        item = item_list[i] if i < len(item_list) else None
        try:
            with repos.session.begin_nested():
                if _item_has_data(item):
                    provider: OcrProvider = _FormOcr(_extraction_from_item(item))
                    cat_uuid = (
                        uuid.UUID(item["category_id"]) if item.get("category_id") else None
                    )
                    pay = (item.get("payment_method") or "out_of_pocket")
                else:
                    provider, cat_uuid, pay = ocr, None, "out_of_pocket"
                _service.add_line(
                    repos=repos,
                    claim=claim,
                    image_bytes=image_bytes,
                    media_type=media_type,
                    ocr=provider,
                    image_dir=image_dir,
                    category_id=cat_uuid,
                    payment_method=pay,
                )
            added += 1
        except (ValidationError, InvalidOperation, OcrError, ClaimError, ValueError) as exc:
            errors.append(f"{name}: {exc}")

    # Mileage trips added on the capture page → mileage lines on the SAME claim. The
    # server recomputes each authoritative distance (never trusts a client km) and
    # records the chosen route + recommended km (longer-than-recommended is flagged).
    for spec in mileage_specs:
        if not isinstance(spec, dict):
            continue
        origin = str(spec.get("origin") or "").strip()
        destination = str(spec.get("destination") or "").strip()
        sdate = str(spec.get("trip_date") or "").strip()
        if not origin or not destination:
            errors.append("mileage: a trip is missing From/To")
            continue
        if not sdate:
            errors.append(f"mileage {origin} → {destination}: a trip date is required")
            continue
        wps = [w for w in (spec.get("waypoints") or []) if isinstance(w, str) and w.strip()]
        try:
            ridx = int(spec.get("route_index") or 0)
        except (TypeError, ValueError):
            ridx = 0
        try:
            with repos.session.begin_nested():
                route, recommended_km = _resolve_route(origin, destination, wps, ridx)
                _service.add_mileage_line(
                    repos=repos, claim=claim, origin=origin, destination=destination,
                    waypoints=wps, route=route, date=sdate or None,
                    rate=deps.get_mileage_rate(), recommended_km=recommended_km,
                )
            added += 1
        except (MapError, ClaimError, ValueError) as exc:
            errors.append(f"mileage {origin} → {destination}: {exc}")

    if added == 0:
        repos.session.rollback()  # no lines → don't leave an empty claim header
        msg = "Could not add any line. " + " · ".join(errors)
        return _render_capture(request, _capture_categories(repos), _events_for(repos), msg)

    _service.submit(repos=repos, claim=claim, actor=_actor(principal), line_count=added)
    return RedirectResponse(f"/claims/{claim.id}/review", status_code=303)


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_route(origin: str, destination: str, wps: list[str], route_index: int):
    """Authoritatively compute the route the claimant chose. Returns
    ``(chosen, recommended_km)``: ``recommended_km`` is always Google's recommended
    route (``routes[0]``) — the cap the chosen route is flagged against. Alternatives
    only exist for a direct trip; an out-of-range index falls back to recommended."""
    options = deps.get_directions().routes(origin, destination, wps)
    idx = route_index if 0 <= route_index < len(options) else 0
    return options[idx], options[0].distance_km


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
        "recommended_km": str(options[0].distance_km),
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
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.get_session_principal),
):
    """Create a ONE-line mileage claim. The server recomputes the distance via the
    Directions provider (authoritative for reimbursement — never trust client km),
    prices it at the per-km rate, and lands on the review screen. ``route_index``
    selects the alternative the claimant picked; the recommended distance is kept so
    a longer-than-recommended route is flagged to the approver."""
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
    try:
        route, recommended_km = _resolve_route(
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
            rate=deps.get_mileage_rate(), recommended_km=recommended_km,
        )
    except (ClaimError, ValueError) as exc:
        repos.session.rollback()
        return _render_capture(request, _capture_categories(repos), _events_for(repos),
                               str(exc), form)
    _service.submit(repos=repos, claim=claim, actor=_actor(principal), line_count=1)
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
        db, secret=settings.jwt_secret, ttl_seconds=settings.jwt_ttl_seconds,
        allow_passwordless=settings.dev_auth_allowed,
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
CLAIMS_PAGE_SIZE = 25


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
    all_claims = repos.claims.list_for_clients(principal.allowed_client_ids, status)
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
    # Per-line OCR bounding boxes for the receipt-highlight overlay (field -> box).
    line_boxes = {str(ln.id): (ln.ocr_boxes or {}) for ln in lines}
    # Mileage lines show a route map instead of a receipt.
    line_mileage = {str(ln.id): ln.mileage for ln in lines if ln.mileage}
    maps_key = get_settings().google_maps_browser_key or get_settings().google_maps_api_key
    # Posting readiness per line (GL + resolvable cost centre) — the audit gate.
    requires_coding = _service._requires_coding(repos, claim)
    coding = {
        ln.id: {
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
    can_review = can_approve(claim, principal) and claim.status == "in_review"
    review_block_reason = None
    if claim.status == "in_review" and not can_review:
        from ..services.sod import SoDViolation, check_can_approve

        try:
            check_can_approve(claim, principal)
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
            "events": repos.audit.chain("claim", claim_id),
            "can_review": can_review,
            "review_block_reason": review_block_reason,
            "can_edit": can_edit,
            "can_resubmit": can_edit and claim.status in ("submitted", "sent_back"),
            "can_release": claim.status in ("approved", "partially_approved")
            and principal.base_role != "viewer",
            # Reopen for amendment — only before the claim has left for another
            # system (released/exported/paid are locked).
            "can_reopen": claim.status in ("approved", "partially_approved")
            and principal.base_role != "viewer"
            and principal.can_access_client(claim.client_id),
            "next_review_id": queue[0].id if queue else None,
            "review_remaining": len(queue),
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
    """Serve the claim's first line image (back-compat; RLS-scoped → 404)."""
    line = repos.claims.first_line(claim_id)
    if line is None or not line.image_path or not os.path.exists(line.image_path):
        raise HTTPException(status_code=404, detail="image not available")
    return FileResponse(line.image_path)


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
    return FileResponse(line.image_path)


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
    for key, value in (("total_amount", total_amount), ("quantity", quantity),
                       ("tax_amount", tax_amount), ("fx_rate", fx_rate)):
        if value != "":
            fields[key] = Decimal(value)
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
        route, recommended_km = _resolve_route(
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
            principal=principal, recommended_km=recommended_km,
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
    edit: uuid.UUID | None = None,
    repos: Repos = Depends(deps.get_web_repos),
    principal: Principal = Depends(deps.require_firm_scope),
) -> HTMLResponse:
    editing = repos.events.get(edit) if edit else None
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
