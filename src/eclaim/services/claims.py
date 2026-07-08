"""Claim lifecycle service: upload → review/edit → approve → release → reverse.

Holds the domain logic for one e-Claim. Persistence goes through the
repositories; carbon maths through :mod:`core.carbon`; the release anchor and
audit chain through :mod:`core.release` / :mod:`core.audit`. The service never
commits — the caller (API route) owns the transaction, so each operation is
all-or-nothing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from core.release import StubSink, StubTSA, canonical_hash

if TYPE_CHECKING:
    from ..auth.principal import Principal

from ..db.models import CarbonHandoff, Claim, Claimant, ClaimLine, Client, ReleaseBatch
from ..ocr.base import Extraction, OcrProvider
from ..repositories import (
    ApprovalMatrixRepository,
    AuditRepository,
    CarbonHandoffRepository,
    CategoryRepository,
    ClaimantRepository,
    ClaimRepository,
    EventRepository,
    ReleaseRepository,
)
from .audit import record_event
from .classify import carbon_relevant_for
from .documents import normalize_image

SOURCE_TYPE = "eclaim"


def _audit_value(v):
    """Coerce a field value to a JSON-safe scalar for an audit ``detail`` (JSONB).
    Decimal/date/datetime become their string form (exact, human-readable); other
    primitives pass through unchanged. Keeps the before/after record serialisable
    without losing precision on money or dates."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


# Receipt date formats seen on real (mostly Malaysian) receipts, day-first. strptime
# matches %b/%B case-insensitively, so "APR"/"Apr"/"apr" all parse. Tried in order;
# the first that consumes the whole (time-stripped) string wins.
_RECEIPT_DATE_FORMATS = (
    "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y",
    "%Y-%m-%d", "%Y/%m/%d",
    "%d%b%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y",
    "%b %d %Y", "%B %d %Y", "%b %d, %Y",
)


def parse_receipt_date(value: str | None) -> dt.date | None:
    """Best-effort parse of an OCR receipt date string into a real ``date`` — used to
    DEFAULT the posting date at capture so a reviewer isn't retyping the receipt date.

    Tolerant of the many printed formats (``02APR2026 04:50PM``, ``23/04/26``,
    ``26 SEP 2025``, ``26 Feb 2026``, ISO). A trailing clock time is stripped first.
    Day-first (DD/MM), matching local receipts. Returns ``None`` when nothing parses
    cleanly — the field stays blank for manual entry rather than guessing a wrong date
    (a wrong posting date is worse than an empty one)."""
    if not value:
        return None
    text = value.strip()
    # Drop a trailing clock time ("... 04:50PM", "... 16:30", "...16:30:00").
    text = re.split(r"\s+\d{1,2}:\d{2}", text, maxsplit=1)[0].strip()
    for fmt in _RECEIPT_DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None

# Claim-level purpose/type (migration 0010). 'general' is the everyday one-off
# claim; the rest describe a multi-day reason. A non-general STANDALONE claim (no
# Event to inherit dates from) requires a start+end date — that is exactly what the
# approver validates (per-diem days, late submission, split/duplicate detection).
CLAIM_TYPES = ("general", "travel", "training", "client_meeting", "other")
DATED_CLAIM_TYPES = tuple(t for t in CLAIM_TYPES if t != "general")

_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class ClaimError(RuntimeError):
    """Base for claim-service errors (mapped to 4xx by the API)."""


class ClaimNotFound(ClaimError):
    pass


class IllegalTransition(ClaimError):
    """An operation not allowed from the claim's current status."""


class AttestationRequired(ClaimError):
    """A claim that reimburses out-of-pocket expense cannot be released until the
    claimant has attested (Appendix A / punch-list P3). The downstream gate that
    closes the hole for every capture path, not just the web form."""


class ClaimService:
    """Stateless: all state is in the repositories passed per call."""

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _store_image(image_dir: Path, image_bytes: bytes, media_type: str) -> tuple[str, str]:
        sha = hashlib.sha256(image_bytes).hexdigest()
        image_dir.mkdir(parents=True, exist_ok=True)
        path = image_dir / f"{sha}{_EXT.get(media_type, '.bin')}"
        if not path.exists():
            path.write_bytes(image_bytes)
        return str(path), sha

    # -- accounting coding (a claim line is a postable source document) ---- #
    # Fields a reviewer may code on a line, beyond the OCR-extracted ones.
    CODING_FIELDS = frozenset({
        "gl_code", "cost_centre_override", "department", "project_code",
        "posting_date", "supplier_tax_id", "tax_amount", "tax_code",
        "tax_inclusive", "fx_rate",
    })

    @staticmethod
    def _recompute_line_money(line: ClaimLine) -> None:
        """Derive ``net_amount`` and ``base_amount`` from the keyed gross/tax/FX so
        the posted figures never drift. Receipts are tax-INCLUSIVE by convention
        (the printed total includes GST), so net = gross − tax; an explicit
        ``tax_inclusive = False`` means the total is already net. ``base_amount`` is
        the home-currency value (× FX when a rate is given, else the gross)."""
        gross = line.total_amount
        tax = line.tax_amount or Decimal("0")
        if gross is None:
            line.net_amount = None
        elif line.tax_inclusive is False:
            line.net_amount = gross
        else:
            line.net_amount = gross - tax
        if gross is None:
            line.base_amount = None
        elif line.fx_rate:
            line.base_amount = (gross * line.fx_rate).quantize(Decimal("0.01"))
        else:
            line.base_amount = gross

    def _resolved_cost_centre(self, repos: "Repos", line: ClaimLine, claim: Claim) -> str | None:
        """The cost centre a line posts to: its own override, else the claimant's,
        else the event's. Mirrors how Finance inherits the dimension."""
        if line.cost_centre_override:
            return line.cost_centre_override
        if claim.submitted_by_claimant_id:
            cm = repos.session.get(Claimant, claim.submitted_by_claimant_id)
            if cm and cm.cost_centre:
                return cm.cost_centre
        if claim.event_id:
            ev = repos.events.get(claim.event_id)
            if ev and ev.cost_centre:
                return ev.cost_centre
        return None

    def _resolved_gl(self, repos: "Repos", line: ClaimLine) -> str | None:
        """The GL account a line posts to: its own override, else the category's
        default ``gl_export_code``."""
        if line.gl_code:
            return line.gl_code
        if line.category_id:
            cat = repos.categories.get_by_id(line.category_id)
            if cat and cat.gl_export_code:
                return cat.gl_export_code
        return None

    def _posting_ready(self, repos: "Repos", line: ClaimLine, claim: Claim) -> bool:
        """A line is postable when it has a resolvable GL account and cost centre —
        the minimum a listed-company GL needs to book the expense."""
        return bool(self._resolved_gl(repos, line)) and bool(
            self._resolved_cost_centre(repos, line, claim)
        )

    @staticmethod
    def _requires_coding(repos: "Repos", claim: Claim) -> bool:
        """Per-client policy: enforce full posting coding before release. Stored on
        ``client.modules.require_posting_coding`` so it is opt-in per tenant (off by
        default; the seed turns it on for a real client)."""
        client = repos.session.get(Client, claim.client_id)
        return bool(client and (client.modules or {}).get("require_posting_coding"))

    @staticmethod
    def _recompute_totals(claim: Claim, lines: list[ClaimLine]) -> None:
        """Roll the line amounts up onto the header: claimed = all lines, approved =
        approved lines, reimbursable = approved out-of-pocket lines (corporate-card
        lines reconcile only, they are never paid back to the employee)."""
        def _sum(ls) -> Decimal | None:
            vals = [x.total_amount for x in ls if x.total_amount is not None]
            return sum(vals, Decimal("0")) if vals else None

        approved = [x for x in lines if x.line_status == "approved"]
        claim.total_claimed = _sum(lines)
        claim.total_approved = _sum(approved) if approved else None
        claim.total_reimbursable = (
            _sum([x for x in approved if x.payment_method == "out_of_pocket"])
            if approved
            else None
        )

    @staticmethod
    def _payload(line: ClaimLine, category) -> dict:
        """Hash-stable RAW payload of ONE carbon-relevant line — the expense data
        forwarded to CarbonNext. NO scope/factor/basis/tCO2e: e-Claim does no carbon
        maths; CarbonNext maps the category + amount/quantity to emissions."""
        return {
            "line_id": str(line.id),
            "claim_id": str(line.claim_id),
            "category": (category.name if category is not None else None),
            "expense_type": line.expense_type,
            "vendor": line.vendor,
            "doc_date": line.doc_date,
            "amount": None if line.total_amount is None else format(line.total_amount, "f"),
            "currency": line.currency,
            "quantity": None if line.quantity is None else format(line.quantity, "f"),
            "unit": line.unit,
        }

    # -- operations -------------------------------------------------------- #
    def start_claim(
        self,
        *,
        repos: "Repos",
        firm_id: uuid.UUID,
        client_id: uuid.UUID,
        title: str | None = None,
        purpose: str | None = None,
        remarks: str | None = None,
        posting_date: dt.date | None = None,
        claim_type: str = "general",
        start_date: dt.date | None = None,
        end_date: dt.date | None = None,
        event_id: uuid.UUID | None = None,
        claimant_ref: str | None = None,
        submitted_by_claimant_id: uuid.UUID | None = None,
        created_by_user_id: uuid.UUID | None = None,
    ) -> Claim:
        """Create an empty in-review claim header (lines added via :meth:`add_line`).

        Pass ``created_by_user_id`` when a firm USER keys the claim interactively
        (the web capture path) — it records the maker so the separation-of-duties
        rule (``approved_by <> created_by``, enforced both in :func:`check_can_approve`
        and the ``ck_claim_sod`` DB CHECK) bites and that user cannot approve their
        own claim. Leave it null for the pure claimant-submission channel (no firm
        user keyed it). No audit event yet — :meth:`upload` / :meth:`submit` records
        'submitted' once the lines are attached.

        The claim ``claim_type`` is compulsory (defaults to 'general' for the
        everyday one-off / API path). A non-general claim with **no Event** must
        carry a start+end date — the approver needs the range for per-diem, late
        submission and duplicate detection. A claim that attaches an Event inherits
        the event's dates, so the claim-level range may stay blank.
        """
        claim_type = claim_type or "general"
        if claim_type not in CLAIM_TYPES:
            raise ClaimError(f"unknown claim type {claim_type!r}")
        if event_id is None and claim_type in DATED_CLAIM_TYPES and not (start_date and end_date):
            raise ClaimError(
                f"a {claim_type.replace('_', ' ')} claim needs a start and end date "
                "(or attach it to an event)"
            )
        if start_date and end_date and end_date < start_date:
            raise ClaimError("the end date is before the start date")

        claim = Claim(
            firm_id=firm_id,
            client_id=client_id,
            claim_no=repos.claims.next_claim_no(year=dt.date.today().year),
            source_channel="upload",
            title=title,
            purpose=purpose,
            remarks=remarks,
            posting_date=posting_date,
            claim_type=claim_type,
            start_date=start_date,
            end_date=end_date,
            event_id=event_id,
            claimant_ref=claimant_ref,
            submitted_by_claimant_id=submitted_by_claimant_id,
            created_by_user_id=created_by_user_id,
            status="in_review",
        )
        return repos.claims.add(claim)

    def add_line(
        self,
        *,
        repos: "Repos",
        claim: Claim,
        image_bytes: bytes,
        media_type: str,
        ocr: OcrProvider,
        image_dir: Path,
        category_id: uuid.UUID | None = None,
        payment_method: str = "out_of_pocket",
        page_images: list[bytes] | None = None,
    ) -> ClaimLine:
        """Store image → OCR → append one line, re-rolling the header totals.
        Category: explicit ``category_id`` (the capture form) wins; else merchant /
        OCR auto-match, else unmapped (a reviewer assigns one). No carbon maths —
        the line just snapshots its category's ``carbon_relevant`` flag (what gets
        forwarded to CarbonNext on release). OCR failure raises before anything is
        persisted — no partial line.

        ``page_images`` (Phase-4 auto-segmentation): the constituent page images when
        several pages of one invoice are grouped into this line. ``image_bytes`` is
        then the stitched composite (OCR reads the whole invoice); the pages are
        stored and recorded in ``pages`` so the reviewer can still split them."""
        # Transcode an iPhone HEIC/HEIF straight-to-service upload to JPEG so OCR and
        # the stored image work (the batch path already normalises before prefetch;
        # this is a no-op there and the guard for the direct API upload path).
        image_bytes, media_type = normalize_image(image_bytes, media_type)
        extraction: Extraction = ocr.extract(image_bytes, media_type)
        image_path, image_sha = self._store_image(image_dir, image_bytes, media_type)
        pages = None
        if page_images:
            pages = []
            for pg in page_images:
                p_path, p_sha = self._store_image(image_dir, pg, "image/png")
                pages.append({"sha": p_sha, "path": p_path})
        if category_id is not None:
            category = repos.categories.get_by_id(category_id)
            if category is None or category.client_id != claim.client_id:
                raise ClaimError("category not found for this client")
        else:
            category = repos.categories.match_by_merchant(
                claim.client_id, extraction.vendor, extraction.expense_type
            )

        line = ClaimLine(
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            claim_id=claim.id,
            line_no=repos.claims.next_line_no(claim.id),
            vendor=extraction.vendor,
            doc_no=extraction.doc_no,
            doc_date=extraction.date,
            # Default the accounting posting date to the receipt's own date so the
            # claim is postable straight from capture; a reviewer can still override.
            # Left NULL if the OCR date is unparseable (better blank than wrong).
            posting_date=parse_receipt_date(extraction.date),
            currency=extraction.currency,
            total_amount=extraction.total_amount,
            tax_amount=extraction.tax_amount,
            tax_code=extraction.tax_code,
            expense_type=extraction.expense_type,
            quantity=extraction.quantity,
            unit=extraction.unit,
            category_id=(category.id if category else None),
            carbon_relevant=carbon_relevant_for(category),
            ocr_confidence=extraction.confidence,
            ocr_boxes=extraction.boxes,
            image_path=image_path,
            image_sha256=image_sha,
            pages=pages,
            payment_method=payment_method,
            reimbursable=(payment_method == "out_of_pocket"),
        )
        # Derive net/base now so the ERP export carries them from capture, not only
        # after a reviewer happens to touch a money field.
        self._recompute_line_money(line)
        repos.claims.add_line(line)
        self._recompute_totals(claim, repos.claims.lines(claim.id))
        return line

    def add_mileage_line(
        self,
        *,
        repos: "Repos",
        claim: Claim,
        origin: str,
        destination: str,
        waypoints: list[str] | None,
        route,
        date: str | None,
        rate: Decimal,
        category_id: uuid.UUID | None = None,
        payment_method: str = "out_of_pocket",
        shortest_km: Decimal | None = None,
    ) -> ClaimLine:
        """Append a MILEAGE line — no receipt, the route is the evidence. The server
        passes the AUTHORITATIVE distance (``route`` from the Directions provider);
        ``amount = km × rate``. ``quantity``/``unit`` carry the km as activity data
        forwarded to CarbonNext (Mileage is carbon-relevant). The route detail lives
        in the ``mileage`` jsonb for the audit pack and the map view.

        ``shortest_km`` is the distance of the SHORTEST route Google offered for the
        same trip (the cheapest to reimburse). When the claimant picked a longer
        route we keep it and flag the overage, so the approver sees they took a
        longer-than-necessary route (policy: pay chosen, flag if longer)."""
        km = route.distance_km
        amount = (km * rate).quantize(Decimal("0.01"))
        # Flag only a material overage vs the shortest route (ignore sub-100 m
        # rounding noise between a fresh recompute and the previewed alternative).
        over_shortest = (
            shortest_km is not None and (km - shortest_km) > Decimal("0.1")
        )
        if category_id is not None:
            category = repos.categories.get_by_id(category_id)
            if category is None or category.client_id != claim.client_id:
                raise ClaimError("category not found for this client")
        else:
            category = repos.categories.match_single(claim.client_id, "mileage")

        line = ClaimLine(
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            claim_id=claim.id,
            line_no=repos.claims.next_line_no(claim.id),
            vendor=f"{origin} → {destination}",
            doc_date=date,
            currency="MYR",
            total_amount=amount,
            expense_type="mileage",
            quantity=km,
            unit="km",
            category_id=(category.id if category else None),
            carbon_relevant=carbon_relevant_for(category),
            business_reason=f"Mileage {km} km",
            mileage={
                "origin": origin,
                "destination": destination,
                "waypoints": list(waypoints or []),
                "distance_km": str(km),
                "rate_per_km": str(rate),
                "polyline": route.polyline,
                "legs": route.legs,
                "shortest_km": (str(shortest_km) if shortest_km is not None else None),
                "over_shortest": over_shortest,
                "route_description": getattr(route, "description", None),
            },
            payment_method=payment_method,
            reimbursable=(payment_method == "out_of_pocket"),
            image_path=None,
            image_sha256=None,
        )
        self._recompute_line_money(line)
        repos.claims.add_line(line)
        self._recompute_totals(claim, repos.claims.lines(claim.id))
        return line

    def add_mileage_to_claim(
        self,
        *,
        repos: "Repos",
        claim_id: uuid.UUID,
        origin: str,
        destination: str,
        waypoints: list[str] | None,
        route,
        date: str | None,
        rate: Decimal,
        actor: str,
        principal: "Principal | None" = None,
        shortest_km: Decimal | None = None,
    ) -> ClaimLine:
        """Add a mileage line to an EXISTING editable claim (the review screen / a
        mixed receipts+mileage claim). Same editability rule as :meth:`edit`; the
        addition is recorded in the audit chain."""
        claim = self.get(repos, claim_id)
        self._require_writer(claim, principal)
        if claim.status not in ("in_review", "submitted", "sent_back"):
            raise IllegalTransition(
                f"cannot add a line to a claim in status {claim.status!r}"
            )
        line = self.add_mileage_line(
            repos=repos, claim=claim, origin=origin, destination=destination,
            waypoints=waypoints, route=route, date=date, rate=rate,
            shortest_km=shortest_km,
        )
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="edited",
            actor=actor,
            detail={"added": "mileage_line", "line_id": str(line.id),
                    "km": str(route.distance_km)},
        )
        return line

    def submit(
        self, *, repos: "Repos", claim: Claim, actor: str, line_count: int,
        attested: bool = False,
    ) -> Claim:
        """Record the 'submitted' audit event for a multi-line claim once its lines
        are attached (the capture path). The header is already in_review.

        When the claim has any out-of-pocket line and ``attested`` is set, stamp the
        attestation (who + when) on the claim — the employee's Appendix-A declaration
        that they paid it themselves and won't be reimbursed elsewhere. The web
        capture route enforces the checkbox *before* the read phase; recording it here
        keeps the stamp on the same transaction as the claim for both the inline and
        the async-ingestion path."""
        lines = repos.claims.lines(claim.id)
        if attested and any(ln.payment_method == "out_of_pocket" for ln in lines):
            claim.attested_by = actor
            claim.attested_at = dt.datetime.now(dt.timezone.utc)
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="submitted",
            actor=actor,
            detail={"line_count": line_count, "attested": bool(claim.attested_by)},
        )
        return claim

    def upload(
        self,
        *,
        repos: "Repos",
        firm_id: uuid.UUID,
        client_id: uuid.UUID,
        image_bytes: bytes,
        media_type: str,
        ocr: OcrProvider,
        image_dir: Path,
        actor: str,
        claimant_ref: str | None = None,
        submitted_by_claimant_id: uuid.UUID | None = None,
        category_id: uuid.UUID | None = None,
        attested: bool = False,
    ) -> Claim:
        """Single-receipt convenience: a claim with exactly one line, then
        'submitted' (the API/back-compat entry point). The web capture path builds
        a multi-line claim directly via :meth:`start_claim` + :meth:`add_line`.

        ``attested`` records the claimant's out-of-pocket declaration (Appendix A):
        when the line is out-of-pocket and ``attested`` is set, the claim is stamped
        (who + when), same as the web capture path. Without it an out-of-pocket claim
        is blocked at release by the attestation gate (punch-list P3)."""
        claim = self.start_claim(
            repos=repos,
            firm_id=firm_id,
            client_id=client_id,
            claimant_ref=claimant_ref,
            submitted_by_claimant_id=submitted_by_claimant_id,
        )
        line = self.add_line(
            repos=repos,
            claim=claim,
            image_bytes=image_bytes,
            media_type=media_type,
            ocr=ocr,
            image_dir=image_dir,
            category_id=category_id,
        )
        if attested and line.payment_method == "out_of_pocket":
            claim.attested_by = actor
            claim.attested_at = dt.datetime.now(dt.timezone.utc)
        record_event(
            repos.audit,
            firm_id=firm_id,
            client_id=client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="submitted",
            actor=actor,
            detail={
                "image_sha256": line.image_sha256, "expense_type": line.expense_type,
                "attested": bool(claim.attested_by),
            },
        )
        return claim

    def get(self, repos: "Repos", claim_id: uuid.UUID) -> Claim:
        claim = repos.claims.get(claim_id)
        if claim is None:
            raise ClaimNotFound(str(claim_id))
        return claim

    def _lock(self, repos: "Repos", claim_id: uuid.UUID) -> Claim:
        """Like :meth:`get` but takes a row lock (``SELECT … FOR UPDATE``) — used by
        the state transitions that must not race (approve/decide/release/reverse), so
        two concurrent requests on one claim serialise rather than both writing."""
        claim = repos.claims.lock_for_update(claim_id)
        if claim is None:
            raise ClaimNotFound(str(claim_id))
        return claim

    @staticmethod
    def _require_writer(claim: Claim, principal: "Principal | None") -> None:
        """Server-side write gate shared by every claim mutation: a Viewer may
        never mutate, and a client-scoped user may only touch claims for clients
        they hold a grant to. ``None`` (a service/integration or unit-test caller)
        skips it; every production web/API route passes the real principal, so the
        UI hiding a button is backed by a real server-side check."""
        if principal is None:
            return
        from .sod import SoDViolation

        if principal.base_role == "viewer" or not principal.can_access_client(claim.client_id):
            raise SoDViolation("not allowed to modify this claim")

    def edit(
        self,
        *,
        repos: "Repos",
        claim_id: uuid.UUID,
        fields: dict,
        actor: str,
        category_id: uuid.UUID | None = None,
        line_id: uuid.UUID | None = None,
        principal: "Principal | None" = None,
    ) -> Claim:
        """Edit one line's fields. Forbidden once released. ``line_id`` selects the
        line; if omitted, the claim's first line is edited (the single-receipt
        path). Header totals are re-rolled after.

        Category resolution: an explicit ``category_id`` assigns that category (a
        reviewer clearing an unmapped line); else editing ``expense_type``/vendor
        re-resolves it; else the line's current category is kept. The line's
        ``carbon_relevant`` flag is re-snapshotted from the resolved category — no
        carbon maths, just the send-to-CarbonNext decision.
        """
        claim = self.get(repos, claim_id)
        self._require_writer(claim, principal)
        # A claim's EXPENSE lines are only editable while under review; once a
        # reviewer has decided it, corrections to the expense (vendor / amount /
        # category) must go back through send-back → resubmit, not silent edits.
        #
        # EXCEPTION — the post-approval, pre-post coding step: after approval but
        # before release, Finance may still set the accounting CODING (GL / cost
        # centre / dept / project / posting date / tax) so the claim becomes
        # postable, WITHOUT reopening the expense itself.
        if claim.status in ("approved", "partially_approved"):
            if category_id is not None or not set(fields).issubset(self.CODING_FIELDS):
                raise IllegalTransition(
                    f"only accounting coding can be edited on an "
                    f"{claim.status.replace('_', ' ')} claim — the expense line is "
                    f"locked; send it back to change the expense"
                )
        elif claim.status not in ("in_review", "submitted", "sent_back"):
            raise IllegalTransition(f"cannot edit a claim in status {claim.status!r}")
        line = repos.claims.line(line_id) if line_id else repos.claims.first_line(claim_id)
        if line is None or line.claim_id != claim.id:
            raise ClaimError("line not found for this claim")

        editable = {
            "vendor", "doc_no", "doc_date", "currency",
            "total_amount", "expense_type", "quantity", "unit",
            "business_reason", "payment_method",
        } | self.CODING_FIELDS
        # Snapshot the current value of each field being changed BEFORE applying,
        # so the audit event records old→new (e.g. amount 500 -> 5000), not just
        # which field was touched — the difference between an answerable and an
        # unanswerable "who changed this" during a dispute.
        before = {k: getattr(line, k, None) for k in fields if k in editable}
        for key, value in fields.items():
            if key in editable:
                setattr(line, key, value)
        if "payment_method" in fields:
            line.reimbursable = line.payment_method == "out_of_pocket"
        # Re-derive net/base whenever any money-affecting field was touched.
        if {"total_amount", "tax_amount", "tax_inclusive", "fx_rate"} & set(fields):
            self._recompute_line_money(line)

        if category_id is not None:
            category = repos.categories.get_by_id(category_id)
            if category is None or category.client_id != line.client_id:
                raise ClaimError("category not found for this client")
        elif "expense_type" in fields or "vendor" in fields:
            category = repos.categories.match_by_merchant(
                line.client_id, line.vendor, line.expense_type
            )
        else:
            category = (
                repos.categories.get_by_id(line.category_id) if line.category_id else None
            )
        line.category_id = category.id if category else None
        line.carbon_relevant = carbon_relevant_for(category)
        self._recompute_totals(claim, repos.claims.lines(claim_id))
        changes = {
            k: {"from": _audit_value(old), "to": _audit_value(getattr(line, k, None))}
            for k, old in before.items()
            if old != getattr(line, k, None)
        }
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="edited",
            actor=actor,
            detail={
                "fields": sorted(fields),
                "changes": changes,
                "line_id": str(line.id),
                "category_id": str(category_id) if category_id else None,
            },
        )
        return claim

    # Document-header (grouping) fields a reviewer/claimant may edit while the
    # claim is still open. These describe the whole claim, not a single line.
    HEADER_FIELDS = frozenset({
        "title", "purpose", "remarks", "posting_date", "department", "project_code",
        "claim_currency",
    })

    def edit_header(
        self,
        *,
        repos: "Repos",
        claim_id: uuid.UUID,
        fields: dict,
        actor: str,
        principal: "Principal | None" = None,
    ) -> Claim:
        """Edit the claim's document-header fields (posting date, purpose, remarks,
        title, cost dimensions). Like :meth:`edit`, allowed only while the claim is
        still open — once decided, changes go back through send-back → resubmit.
        Empty strings clear a field (stored as NULL); typed values (e.g. a parsed
        ``posting_date``) are set as-is. Records one ``edited`` audit event."""
        claim = self.get(repos, claim_id)
        self._require_writer(claim, principal)
        if claim.status not in ("in_review", "submitted", "sent_back"):
            raise IllegalTransition(f"cannot edit a claim in status {claim.status!r}")
        touched: list[str] = []
        changes: dict[str, dict] = {}
        for key, value in fields.items():
            if key not in self.HEADER_FIELDS:
                continue
            old = getattr(claim, key, None)
            if isinstance(value, str):
                value = value.strip() or None
            setattr(claim, key, value)
            touched.append(key)
            if old != value:
                changes[key] = {"from": _audit_value(old), "to": _audit_value(value)}
        if touched:
            record_event(
                repos.audit,
                firm_id=claim.firm_id,
                client_id=claim.client_id,
                entity_type="claim",
                entity_id=claim.id,
                event_type="edited",
                actor=actor,
                detail={"header_fields": sorted(touched), "changes": changes},
            )
        return claim

    # -- merge / split (correct PDF auto-segmentation) --------------------- #
    # Both are gated by the per-client ``allow_document_split`` policy AND only while
    # the claim is editable, and both record an audit event. They are inverse
    # operations over a line's constituent PAGES: merge folds several lines' pages
    # into one line; split expands a multi-page line back into one line per page.

    @staticmethod
    def _allow_document_split(repos: "Repos", claim: Claim) -> bool:
        client = repos.session.get(Client, claim.client_id)
        return bool(client and (client.modules or {}).get("allow_document_split"))

    @staticmethod
    def _line_pages(line: ClaimLine) -> list[dict]:
        """The line's constituent page images: its ``pages`` list if it was merged,
        else the single stored image (or empty for a mileage/imageless line)."""
        if line.pages:
            return [dict(p) for p in line.pages]
        if line.image_path:
            return [{"sha": line.image_sha256, "path": line.image_path}]
        return []

    @staticmethod
    def _renumber(repos: "Repos", ordered: list[ClaimLine]) -> None:
        """Reassign line_no = 1..n in the given order. Two-phase (temp negatives
        first) so the per-claim uniqueness constraint never trips mid-update."""
        for i, ln in enumerate(ordered):
            ln.line_no = -(i + 1)
        repos.session.flush()
        for i, ln in enumerate(ordered):
            ln.line_no = i + 1
        repos.session.flush()

    def _guard_split_merge(self, repos: "Repos", claim: Claim, principal) -> None:
        self._require_writer(claim, principal)
        if claim.status not in ("in_review", "submitted", "sent_back"):
            raise IllegalTransition(
                f"cannot merge/split lines on a claim in status {claim.status!r} — "
                f"send it back to change the lines"
            )
        if not self._allow_document_split(repos, claim):
            raise IllegalTransition("document merge/split is not enabled for this client")

    def merge_lines(
        self,
        *,
        repos: "Repos",
        claim_id: uuid.UUID,
        line_ids: list,
        actor: str,
        image_dir: Path,
        principal: "Principal | None" = None,
    ) -> Claim:
        """Merge several lines that are really pages of ONE invoice into a single
        line. The survivor (lowest line_no) absorbs the others' page images, stitched
        into one composite for display; its fields are kept as-is (NOT summed — pages
        of one invoice — the reviewer re-verifies the total). Deleted lines' pages are
        retained on the survivor's ``pages`` so it can be split again."""
        from .documents import stitch_pages

        claim = self.get(repos, claim_id)
        self._guard_split_merge(repos, claim, principal)
        ids = {uuid.UUID(str(i)) for i in line_ids}
        if len(ids) < 2:
            raise ClaimError("select at least two lines to merge")
        selected = [ln for ln in repos.claims.lines(claim_id) if ln.id in ids]
        if len(selected) != len(ids):
            raise ClaimError("some selected lines were not found on this claim")
        if any(ln.mileage for ln in selected):
            raise ClaimError("mileage lines cannot be merged")
        selected.sort(key=lambda ln: ln.line_no)
        primary = selected[0]

        pages: list[dict] = []
        for ln in selected:
            pages.extend(self._line_pages(ln))
        if len(pages) < 2:
            raise ClaimError("nothing to merge — the selected lines have no images")
        composite = stitch_pages([Path(p["path"]).read_bytes() for p in pages])
        path, sha = self._store_image(image_dir, composite, "image/jpeg")
        primary.image_path = path
        primary.image_sha256 = sha
        primary.pages = pages
        primary.ocr_boxes = None   # per-image boxes no longer map onto the composite

        merged_nos = [ln.line_no for ln in selected]
        for ln in selected[1:]:
            repos.claims.delete_line(ln)
        remaining = repos.claims.lines(claim_id)
        self._renumber(repos, remaining)
        self._recompute_totals(claim, repos.claims.lines(claim_id))
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="lines_merged",
            actor=actor,
            detail={"kept_line_id": str(primary.id), "merged_line_nos": merged_nos,
                    "page_count": len(pages)},
        )
        return claim

    def split_line(
        self,
        *,
        repos: "Repos",
        claim_id: uuid.UUID,
        line_id: uuid.UUID,
        actor: str,
        principal: "Principal | None" = None,
    ) -> Claim:
        """Split a multi-page line back into one line per page. The first page stays
        on the original line; each remaining page becomes a new line copying the
        original's fields (status reset to pending — the reviewer verifies each).
        Only a line with ≥2 pages is splittable (a single scanned image holding two
        invoices would need region-cropping — out of scope)."""
        claim = self.get(repos, claim_id)
        self._guard_split_merge(repos, claim, principal)
        line = repos.claims.line(uuid.UUID(str(line_id)))
        if line is None or line.claim_id != claim.id:
            raise ClaimError("line not found for this claim")
        pages = self._line_pages(line)
        if len(pages) < 2:
            raise ClaimError("only a multi-page line can be split into pages")

        first, rest = pages[0], pages[1:]
        line.image_path = first["path"]
        line.image_sha256 = first["sha"]
        line.pages = None
        line.ocr_boxes = None

        new_lines: list[ClaimLine] = []
        for offset, pg in enumerate(rest, 1):
            new = ClaimLine(
                firm_id=line.firm_id,
                client_id=line.client_id,
                claim_id=claim.id,
                line_no=-(1000 + offset),   # temp unique; _renumber fixes the order
                vendor=line.vendor,
                doc_no=line.doc_no,
                doc_date=line.doc_date,
                currency=line.currency,
                total_amount=line.total_amount,
                expense_type=line.expense_type,
                quantity=line.quantity,
                unit=line.unit,
                category_id=line.category_id,
                carbon_relevant=line.carbon_relevant,
                image_path=pg["path"],
                image_sha256=pg["sha"],
                pages=None,
                payment_method=line.payment_method,
                reimbursable=line.reimbursable,
                line_status="pending",
            )
            repos.claims.add_line(new)
            new_lines.append(new)

        # Keep the split-off pages adjacent to the original line, then renumber 1..n.
        ordered: list[ClaimLine] = []
        for ln in sorted(repos.claims.lines(claim_id), key=lambda x: x.line_no):
            if ln in new_lines:
                continue
            ordered.append(ln)
            if ln.id == line.id:
                ordered.extend(new_lines)
        self._renumber(repos, ordered)
        self._recompute_totals(claim, repos.claims.lines(claim_id))
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="line_split",
            actor=actor,
            detail={"source_line_id": str(line.id), "into_pages": len(pages)},
        )
        return claim

    def approve(
        self,
        *,
        repos: "Repos",
        claim_id: uuid.UUID,
        actor: str,
        approver: "Principal | None" = None,
    ) -> Claim:
        """Approve an in-review claim. When an ``approver`` principal is given,
        the SoD/authority guard runs and ``approved_by_user_id`` is recorded."""
        claim = self._lock(repos, claim_id)
        if claim.status != "in_review":
            raise IllegalTransition(f"cannot approve a claim in status {claim.status!r}")
        if approver is not None:
            from .sod import authorize_approval

            authorize_approval(repos, claim, approver, action="approve")
            claim.approved_by_user_id = approver.user_id
        lines = repos.claims.lines(claim_id)
        for ln in lines:
            ln.line_status = "approved"
        self._recompute_totals(claim, lines)
        claim.status = "approved"
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="approved",
            actor=actor,
        )
        return claim

    def decide(
        self,
        *,
        repos: "Repos",
        claim_id: uuid.UUID,
        reviewer: "Principal",
        decisions: dict[uuid.UUID, tuple[str, str | None]],
        actor: str,
        note: str | None = None,
    ) -> Claim:
        """Partial approval — ONE reviewer action, per-line outcomes.

        ``decisions`` maps line_id → (line_status, reason). A line set to 'queried'
        or 'rejected' must carry a reason. The SoD/authority guard runs ONCE on the
        header. The header status rolls up from the line outcomes:

        * every line approved                  → ``approved``
        * any line queried                     → ``sent_back`` (returns for rework)
        * every (decided) line rejected        → ``rejected``
        * a mix of approved + rejected         → ``partially_approved``
        """
        claim = self._lock(repos, claim_id)
        if claim.status != "in_review":
            raise IllegalTransition(f"cannot decide a claim in status {claim.status!r}")
        from .sod import authorize_approval

        authorize_approval(repos, claim, reviewer, action="decide")

        lines = repos.claims.lines(claim_id)
        by_id = {ln.id: ln for ln in lines}
        for line_id, (line_status, reason) in decisions.items():
            ln = by_id.get(line_id)
            if ln is None:
                raise ClaimError("line not found for this claim")
            if line_status not in ("approved", "queried", "rejected"):
                raise ClaimError(f"invalid line decision {line_status!r}")
            if line_status in ("queried", "rejected") and not (reason and reason.strip()):
                raise ClaimError(f"a {line_status} line needs a reason")
            ln.line_status = line_status
            ln.line_reason = reason or None

        statuses = {ln.line_status for ln in lines}
        if "queried" in statuses or "pending" in statuses:
            new_status = "sent_back"
        elif statuses == {"rejected"}:
            new_status = "rejected"
        elif "rejected" in statuses:
            new_status = "partially_approved"
        else:
            new_status = "approved"

        if new_status in ("approved", "partially_approved"):
            claim.approved_by_user_id = reviewer.user_id
        claim.approver_note = note
        self._recompute_totals(claim, lines)
        claim.status = new_status
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="decided",
            actor=actor,
            detail={
                "status": new_status,
                "note": note,
                "lines": {
                    str(lid): {"status": st, "reason": rs}
                    for lid, (st, rs) in decisions.items()
                },
            },
        )
        return claim

    def send_back(
        self,
        *,
        repos: "Repos",
        claim_id: uuid.UUID,
        reviewer: "Principal",
        reason: str | None = None,
    ) -> Claim:
        """Return an in-review claim to the submitter for rework
        (in_review -> submitted). Guarded by the same reviewer check as
        approval (authority + SoD); the ``reason`` lives in the audit trail, not
        a column, so a sent-back claim re-enters the queue via :meth:`resubmit`."""
        claim = self.get(repos, claim_id)
        if claim.status != "in_review":
            raise IllegalTransition(f"cannot send back a claim in status {claim.status!r}")
        from .sod import authorize_approval

        authorize_approval(repos, claim, reviewer, action="send_back")
        for ln in repos.claims.lines(claim_id):
            ln.line_status = "pending"
        claim.status = "submitted"
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="sent_back",
            actor=reviewer.email or str(reviewer.user_id),
            detail={"reason": reason},
        )
        return claim

    def reject(
        self,
        *,
        repos: "Repos",
        claim_id: uuid.UUID,
        reviewer: "Principal",
        reason: str | None = None,
    ) -> Claim:
        """Reject an in-review claim outright (in_review -> rejected, terminal).
        Same reviewer guard as approval; the ``reason`` is recorded in the audit
        trail. A rejected claim cannot be approved, sent back, or resubmitted."""
        claim = self.get(repos, claim_id)
        if claim.status != "in_review":
            raise IllegalTransition(f"cannot reject a claim in status {claim.status!r}")
        from .sod import authorize_approval

        authorize_approval(repos, claim, reviewer, action="reject")
        for ln in repos.claims.lines(claim_id):
            ln.line_status = "rejected"
        claim.status = "rejected"
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="rejected",
            actor=reviewer.email or str(reviewer.user_id),
            detail={"reason": reason},
        )
        return claim

    def unapprove(
        self, *, repos: "Repos", claim_id: uuid.UUID, reviewer: "Principal", actor: str
    ) -> Claim:
        """Reopen an approved/partially-approved claim back into review so it can be
        amended — allowed ONLY while the claim has not yet left for another system.
        Once released/exported/paid the data is integrated downstream and the claim
        is locked (correct via a reversal, not a reopen).

        Clears the approval (approver + per-line decisions reset to pending) and
        re-rolls the totals; the reviewer needs client access and may not be a
        viewer (no SoD self-check — reopening is not a sign-off)."""
        claim = self.get(repos, claim_id)
        if claim.status not in ("approved", "partially_approved"):
            raise IllegalTransition(f"cannot reopen a claim in status {claim.status!r}")
        from .sod import SoDViolation

        if reviewer.base_role == "viewer" or not reviewer.can_access_client(claim.client_id):
            raise SoDViolation("not allowed to reopen this claim")

        lines = repos.claims.lines(claim_id)
        for ln in lines:
            ln.line_status = "pending"
            ln.line_reason = None
        claim.approved_by_user_id = None
        claim.approver_note = None
        self._recompute_totals(claim, lines)
        claim.status = "in_review"
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="unapproved",
            actor=actor,
        )
        return claim

    def mark_paid(
        self, *, repos: "Repos", claim_id: uuid.UUID, actor: str,
        principal: "Principal | None" = None,
    ) -> Claim:
        """Settle the reimbursement — the employee has been paid back (→ ``paid``). A
        terminal money event, so it's writer-gated and audited.

        Allowed only AFTER release (released / exported): ``release()`` accepts only
        approved claims and ``paid`` is terminal, so paying an approved-but-unreleased
        claim would permanently strand its CarbonNext handoff — and, because the
        attestation gate lives at release, money would leave without the claimant's
        out-of-pocket declaration ever being enforced. Release first, then pay.

        Settlement SoD: the user who keyed the claim may not also record its payment
        (payer ≠ maker — mirrors the approve-side maker≠checker rule)."""
        claim = self._lock(repos, claim_id)
        self._require_writer(claim, principal)
        if (
            principal is not None
            and claim.created_by_user_id is not None
            and claim.created_by_user_id == principal.user_id
        ):
            from .sod import SoDViolation

            raise SoDViolation("the user who created a claim cannot record its payment")
        if claim.status not in ("released", "exported"):
            raise IllegalTransition(
                f"cannot mark a claim in status {claim.status!r} as paid — "
                "release it first (the attestation gate and CarbonNext handoff live at release)"
            )
        prior = claim.status
        claim.status = "paid"
        record_event(
            repos.audit, firm_id=claim.firm_id, client_id=claim.client_id,
            entity_type="claim", entity_id=claim.id, event_type="paid", actor=actor,
            detail={"from_status": prior},
        )
        repos.session.flush()
        return claim

    def resubmit(
        self, *, repos: "Repos", claim_id: uuid.UUID, actor: str,
        principal: "Principal | None" = None,
    ) -> Claim:
        """Re-enter a sent-back claim into the review queue
        (submitted -> in_review), e.g. after its keyed fields were corrected.
        Not a sign-off, so no SoD self-check — but a Viewer still may not do it."""
        claim = self.get(repos, claim_id)
        self._require_writer(claim, principal)
        if claim.status != "submitted":
            raise IllegalTransition(f"cannot resubmit a claim in status {claim.status!r}")
        claim.status = "in_review"
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="resubmitted",
            actor=actor,
        )
        return claim

    def attest(
        self, *, repos: "Repos", claim_id: uuid.UUID, actor: str,
        principal: "Principal | None" = None,
    ) -> Claim:
        """Record the claimant's out-of-pocket attestation on an EXISTING claim
        (Appendix A / punch-list R2) — the after-the-fact re-attest path.

        Attestation is normally stamped at capture (the web checkbox) or submit
        (``attested=True``). A claim created before that gate existed, or through a
        channel that couldn't collect it (pre-P3 API upload / legacy mileage), carries
        NULL attestation and is then PERMANENTLY blocked at release by
        :class:`AttestationRequired`. This lets the claimant declare after the fact so a
        legitimate stuck claim can proceed — WITHOUT a backfill migration stamping an
        attestation nobody actually made (that would forge the very evidence the
        control exists to capture).

        Guards:
        * a Viewer / non-grant-holder may not attest (``_require_writer``);
        * the claim must reimburse out-of-pocket spend (otherwise there is nothing to
          attest to);
        * it must not already be attested — the original attester + timestamp are
          evidence and are never silently overwritten;
        * it must not have left for release (released/exported/paid) or be terminal
          (rejected): attestation must PRECEDE release.

        Locks the row (``FOR UPDATE``) so it serialises with a concurrent release."""
        claim = self._lock(repos, claim_id)
        self._require_writer(claim, principal)
        if claim.status in ("released", "exported", "paid", "rejected"):
            raise IllegalTransition(
                f"cannot attest a claim in status {claim.status!r} — attestation must "
                "precede release"
            )
        lines = repos.claims.lines(claim_id)
        if not any(ln.payment_method == "out_of_pocket" for ln in lines):
            raise ClaimError(
                "nothing to attest: this claim has no out-of-pocket lines"
            )
        if claim.attested_by is not None:
            raise IllegalTransition("this claim is already attested")

        claim.attested_by = actor
        claim.attested_at = dt.datetime.now(dt.timezone.utc)
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="attested",
            actor=actor,
            detail={"reattest": True, "status": claim.status},
        )
        repos.session.flush()
        return claim

    def _category_of(self, repos: "Repos", line: ClaimLine):
        return repos.categories.get_by_id(line.category_id) if line.category_id else None

    def release(
        self, *, repos: "Repos", claim_id: uuid.UUID, actor: str,
        principal: "Principal | None" = None,
    ) -> ReleaseBatch:
        """Release an approved claim — FORWARD its carbon-relevant approved lines to
        CarbonNext as RAW expense data (idempotent). Each becomes one
        ``carbon_handoff`` row (category, amount, currency, quantity, unit, vendor,
        date). NO scope/factor/tCO2e — e-Claim does no carbon maths; CarbonNext maps
        and computes. Non-relevant lines never forward; the claim still releases (and
        ALL lines still export to ERP)."""
        claim = self._lock(repos, claim_id)
        self._require_writer(claim, principal)
        lines = repos.claims.lines(claim_id)

        existing = repos.handoffs.first_for_lines([ln.id for ln in lines])
        if existing is not None:
            # Already released — idempotent no-op, return the original batch.
            return repos.session.get(ReleaseBatch, existing.release_batch_id)

        # A released claim with NO carbon-relevant lines wrote no handoff to key on;
        # recover its batch by the deterministic content hash so a re-release is
        # still an idempotent no-op (not an IllegalTransition).
        if claim.status in ("released", "exported", "paid"):
            prior = repos.releases.batch_by_hash(
                claim.client_id, canonical_hash([{"claim_id": str(claim.id)}])
            )
            if prior is not None:
                return prior

        # Partial approvals are releasable too — release filters to approved lines,
        # so the approved portion still forwards to CarbonNext and exports to the ERP.
        if claim.status not in ("approved", "partially_approved"):
            raise IllegalTransition(f"cannot release a claim in status {claim.status!r}")

        # Attestation gate (Appendix A / punch-list P3): a claim that reimburses an
        # employee for out-of-pocket spend cannot be released until they have attested
        # they paid it themselves and won't double-claim. This is the downstream
        # chokepoint — it closes the hole for EVERY capture path (web capture, JSON
        # API upload, legacy mileage), present and future, not just the web form that
        # happens to collect the checkbox.
        if claim.attested_by is None and any(
            ln.line_status == "approved" and ln.payment_method == "out_of_pocket"
            for ln in lines
        ):
            raise AttestationRequired(
                "cannot release: this claim reimburses out-of-pocket expense but has "
                "no attestation on file — the claimant must attest before release"
            )

        # Posting gate (per-client policy): a claim cannot be released to accounting
        # until every approved line is fully coded (GL + cost centre).
        if self._requires_coding(repos, claim):
            uncoded = [
                ln.line_no for ln in lines
                if ln.line_status == "approved" and not self._posting_ready(repos, ln, claim)
            ]
            if uncoded:
                raise IllegalTransition(
                    "cannot release: line(s) "
                    + ", ".join(str(n) for n in uncoded)
                    + " need a GL code and cost centre before posting"
                )

        relevant = [
            ln for ln in lines if ln.line_status == "approved" and ln.carbon_relevant
        ]
        items = [(ln, self._category_of(repos, ln)) for ln in relevant]
        payloads = [self._payload(ln, cat) for ln, cat in items]
        digest = canonical_hash(payloads or [{"claim_id": str(claim.id)}])
        carbon_ref = f"CARB-{digest[:12].upper()}"
        token = StubTSA().stamp(digest)
        # Parent-document gross per source document (F-B): computed over ALL lines
        # (carbon + non-carbon) so a forwarded line carries the whole bill's total.
        doc_totals = _doc_gross_totals(lines)

        # The whole release is written in a savepoint. Concurrent transitions on this
        # claim already serialise on the FOR UPDATE lock above, so the loser normally
        # takes an idempotent early-return; the UNIQUE(client_id, batch_hash) on
        # release_batch (and the handoff idempotency key) is the DB-level backstop that
        # makes a double-release impossible even if the lock were ever bypassed. Map
        # that collision to the same idempotent no-op instead of a 500.
        try:
            with repos.session.begin_nested():
                batch = repos.releases.add_batch(
                    ReleaseBatch(
                        firm_id=claim.firm_id,
                        client_id=claim.client_id,
                        source_type=SOURCE_TYPE,
                        created_by=actor,
                        batch_hash=digest,
                        tsa_token=token,
                        record_count=len(relevant),
                        total_tco2e=None,  # e-Claim does not compute tonnage
                        status="released",
                    )
                )
                for ln, cat in items:
                    repos.handoffs.add(
                        CarbonHandoff(
                            firm_id=claim.firm_id,
                            client_id=claim.client_id,
                            claim_id=claim.id,
                            line_id=ln.id,
                            release_batch_id=batch.id,
                            category_id=ln.category_id,
                            category_name=(cat.name if cat else None),
                            expense_type=ln.expense_type,
                            vendor=ln.vendor,
                            doc_date=_handoff_date(ln.doc_date),
                            amount=ln.total_amount,
                            currency=ln.currency,
                            net_amount=ln.net_amount,
                            tax_amount=ln.tax_amount,
                            base_amount=ln.base_amount,
                            quantity=ln.quantity,
                            unit=ln.unit,
                            cost_centre=self._resolved_cost_centre(repos, ln, claim),
                            department=ln.department or claim.department,
                            doc_no=ln.doc_no,
                            doc_gross_total=doc_totals.get(_doc_key(ln)),
                            direction="forward",
                            idempotency_key=_idempotency_key(claim.client_id, ln.id),
                            carbon_ref=f"CARB-{canonical_hash([self._payload(ln, cat)])[:12].upper()}",
                        )
                    )
                claim.status = "released"

                released = record_event(
                    repos.audit,
                    firm_id=claim.firm_id,
                    client_id=claim.client_id,
                    entity_type="claim",
                    entity_id=claim.id,
                    event_type="released",
                    actor=actor,
                    detail={"batch_hash": digest, "carbon_ref": carbon_ref, "record_count": len(relevant)},
                )
                record_event(
                    repos.audit,
                    firm_id=claim.firm_id,
                    client_id=claim.client_id,
                    entity_type="claim",
                    entity_id=claim.id,
                    event_type="tsa_anchored",
                    actor="system",
                    detail={"tsa_token": token},
                    prev_hash=released.hash,
                )
        except IntegrityError:
            prior = self._recover_release(repos, claim, digest)
            if prior is not None:
                return prior
            raise
        StubSink().post(digest, len(relevant))
        return batch

    def reverse(
        self, *, repos: "Repos", claim_id: uuid.UUID, actor: str,
        principal: "Principal | None" = None,
    ) -> ReleaseBatch:
        """Correct a released claim by forwarding REVERSAL rows to CarbonNext — one
        per relevant line of the original release, with negated amount/quantity and
        ``direction='reversal'``. Never edits or deletes the originals; a correction
        is a new, opposite-signed handoff batch."""
        claim = self._lock(repos, claim_id)
        self._require_writer(claim, principal)
        if claim.status != "released":
            raise IllegalTransition("only a released claim can be reversed")

        lines = repos.claims.lines(claim_id)
        relevant = [
            ln for ln in lines if ln.line_status == "approved" and ln.carbon_relevant
        ]
        rev_keys = [
            _idempotency_key(claim.client_id, ln.id, suffix="reversal") for ln in relevant
        ]
        if relevant and repos.handoffs.by_idempotency(rev_keys[0]) is not None:
            raise IllegalTransition("claim already reversed")

        items = [(ln, self._category_of(repos, ln)) for ln in relevant]
        payloads = [
            self._payload(ln, cat) | {"reversal_of": str(ln.id)} for ln, cat in items
        ]
        digest = canonical_hash(payloads or [{"reversal_of": str(claim.id)}])
        # Same parent-doc gross as the forward rows carried — direction-independent
        # context (NOT negated), so a reversal reconciles to the same bill (F-B).
        doc_totals = _doc_gross_totals(lines)

        # Same savepoint + idempotent-recovery pattern as release(): the FOR UPDATE
        # lock serialises, and UNIQUE(client_id, batch_hash) is the backstop that
        # stops a concurrent double-reversal (the zero-relevant-line case has no
        # handoff idempotency key to collide on, so the batch hash is what guards it).
        try:
            with repos.session.begin_nested():
                batch = repos.releases.add_batch(
                    ReleaseBatch(
                        firm_id=claim.firm_id,
                        client_id=claim.client_id,
                        source_type=SOURCE_TYPE,
                        created_by=actor,
                        batch_hash=digest,
                        tsa_token=StubTSA().stamp(digest),
                        record_count=len(relevant),
                        total_tco2e=None,
                        status="released",
                    )
                )
                for (ln, cat), idem in zip(items, rev_keys):
                    repos.handoffs.add(
                        CarbonHandoff(
                            firm_id=claim.firm_id,
                            client_id=claim.client_id,
                            claim_id=claim.id,
                            line_id=ln.id,
                            release_batch_id=batch.id,
                            category_id=ln.category_id,
                            category_name=(cat.name if cat else None),
                            expense_type=ln.expense_type,
                            vendor=ln.vendor,
                            doc_date=_handoff_date(ln.doc_date),
                            amount=None if ln.total_amount is None else -ln.total_amount,
                            currency=ln.currency,
                            net_amount=None if ln.net_amount is None else -ln.net_amount,
                            tax_amount=None if ln.tax_amount is None else -ln.tax_amount,
                            base_amount=None if ln.base_amount is None else -ln.base_amount,
                            quantity=None if ln.quantity is None else -ln.quantity,
                            unit=ln.unit,
                            cost_centre=self._resolved_cost_centre(repos, ln, claim),
                            department=ln.department or claim.department,
                            doc_no=ln.doc_no,
                            doc_gross_total=doc_totals.get(_doc_key(ln)),
                            direction="reversal",
                            idempotency_key=idem,
                            carbon_ref=f"CARB-REV-{digest[:12].upper()}",
                        )
                    )
                record_event(
                    repos.audit,
                    firm_id=claim.firm_id,
                    client_id=claim.client_id,
                    entity_type="claim",
                    entity_id=claim.id,
                    event_type="reversed",
                    actor=actor,
                    detail={"batch_hash": digest, "record_count": len(relevant)},
                )
        except IntegrityError:
            prior = self._recover_release(repos, claim, digest)
            if prior is not None:
                return prior
            raise
        return batch

    def _recover_release(self, repos: "Repos", claim: Claim, digest: str):
        """After a UNIQUE/idempotency collision on release or reverse, discard this
        transaction's rolled-back in-memory changes and return the batch the winning
        concurrent transaction already committed (keyed by the deterministic content
        hash), so the caller reports an idempotent no-op rather than a 500. Returns
        None only if no such batch is found (a genuine, unrelated integrity error)."""
        repos.session.expire(claim)
        return repos.releases.batch_by_hash(claim.client_id, digest)


@dataclass
class Repos:
    """Bundle of repositories sharing one session, for one request/operation."""

    session: object
    claims: ClaimRepository
    categories: CategoryRepository
    claimants: ClaimantRepository
    releases: ReleaseRepository
    handoffs: CarbonHandoffRepository
    audit: AuditRepository
    events: EventRepository
    approvals: ApprovalMatrixRepository

    @classmethod
    def for_session(cls, session) -> "Repos":
        return cls(
            session=session,
            claims=ClaimRepository(session),
            categories=CategoryRepository(session),
            claimants=ClaimantRepository(session),
            releases=ReleaseRepository(session),
            handoffs=CarbonHandoffRepository(session),
            audit=AuditRepository(session),
            events=EventRepository(session),
            approvals=ApprovalMatrixRepository(session),
        )


def _idempotency_key(client_id: uuid.UUID, claim_id: uuid.UUID, suffix: str = "") -> str:
    raw = f"{client_id}{claim_id}{suffix}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _handoff_date(value: str | None) -> str | None:
    """The doc_date the handoff forwards: NORMALIZED to ISO when the OCR string
    parses ('26 SEP 2025' → '2025-09-26'), else the raw text — CarbonNext should
    never have to re-implement Malaysian receipt-date parsing (F-D contract), but a
    date we can't parse is still better forwarded verbatim than dropped."""
    parsed = parse_receipt_date(value)
    return parsed.isoformat() if parsed else value


def _doc_key(line: ClaimLine) -> str:
    """The identity of the source DOCUMENT a line came from (F-B). Lines split from one
    receipt share its ``doc_no`` (split copies the field), so they group together; a
    line with no ``doc_no`` is its own document, keyed by its id so two blank-doc lines
    never merge into one phantom document."""
    return line.doc_no if line.doc_no else f"__line__{line.id}"


def _doc_gross_totals(lines: list[ClaimLine]) -> dict[str, Decimal]:
    """Gross total per source document across ALL of a claim's lines — carbon AND
    non-carbon (F-B). Stamped onto every forwarded/reversed handoff row so the forwarded
    amount (carbon lines only) can be reconciled against the whole bill by reference.
    Sums ``total_amount`` (gross); a line with no amount contributes nothing."""
    totals: dict[str, Decimal] = {}
    for ln in lines:
        if ln.total_amount is None:
            totals.setdefault(_doc_key(ln), Decimal("0"))
            continue
        totals[_doc_key(ln)] = totals.get(_doc_key(ln), Decimal("0")) + ln.total_amount
    return totals
