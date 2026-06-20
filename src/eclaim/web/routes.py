"""Web pages: Capture, Review, Ledger. Thin server-rendered views over the API.

The pages read data server-side (via the same repositories) and post mutations
to the JSON API with small inline fetch calls, so there's one source of truth.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..repositories import LedgerRepository
from ..services.claims import ClaimService, Repos
from ..api import deps

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

router = APIRouter(tags=["web"])
_service = ClaimService()


@router.get("/", response_class=HTMLResponse)
def capture_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "capture.html", {})


@router.get("/claims/{claim_id}/review", response_class=HTMLResponse)
def review_page(
    request: Request, claim_id: uuid.UUID, repos: Repos = Depends(deps.get_repos)
) -> HTMLResponse:
    claim = repos.claims.get(claim_id)
    events = repos.audit.chain("claim", claim_id)
    return templates.TemplateResponse(
        request, "review.html", {"claim": claim, "events": events}
    )


@router.get("/ledger", response_class=HTMLResponse)
def ledger_page(request: Request, repos: Repos = Depends(deps.get_repos)) -> HTMLResponse:
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
