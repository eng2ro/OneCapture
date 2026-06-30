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
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from core.release import StubSink, StubTSA, canonical_hash

if TYPE_CHECKING:
    from ..auth.principal import Principal

from ..db.models import CarbonHandoff, Claim, Claimant, ClaimLine, Client, ReleaseBatch
from ..ocr.base import Extraction, OcrProvider
from ..repositories import (
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

SOURCE_TYPE = "eclaim"

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
    ) -> ClaimLine:
        """Store image → OCR → append one line, re-rolling the header totals.
        Category: explicit ``category_id`` (the capture form) wins; else merchant /
        OCR auto-match, else unmapped (a reviewer assigns one). No carbon maths —
        the line just snapshots its category's ``carbon_relevant`` flag (what gets
        forwarded to CarbonNext on release). OCR failure raises before anything is
        persisted — no partial line."""
        extraction: Extraction = ocr.extract(image_bytes, media_type)
        image_path, image_sha = self._store_image(image_dir, image_bytes, media_type)
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
            currency=extraction.currency,
            total_amount=extraction.total_amount,
            expense_type=extraction.expense_type,
            quantity=extraction.quantity,
            unit=extraction.unit,
            category_id=(category.id if category else None),
            carbon_relevant=carbon_relevant_for(category),
            ocr_confidence=extraction.confidence,
            ocr_boxes=extraction.boxes,
            image_path=image_path,
            image_sha256=image_sha,
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

    def submit(self, *, repos: "Repos", claim: Claim, actor: str, line_count: int) -> Claim:
        """Record the 'submitted' audit event for a multi-line claim once its lines
        are attached (the capture path). The header is already in_review."""
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="submitted",
            actor=actor,
            detail={"line_count": line_count},
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
    ) -> Claim:
        """Single-receipt convenience: a claim with exactly one line, then
        'submitted' (the API/back-compat entry point). The web capture path builds
        a multi-line claim directly via :meth:`start_claim` + :meth:`add_line`."""
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
        record_event(
            repos.audit,
            firm_id=firm_id,
            client_id=client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="submitted",
            actor=actor,
            detail={"image_sha256": line.image_sha256, "expense_type": line.expense_type},
        )
        return claim

    def get(self, repos: "Repos", claim_id: uuid.UUID) -> Claim:
        claim = repos.claims.get(claim_id)
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
        for key, value in fields.items():
            if key not in self.HEADER_FIELDS:
                continue
            if isinstance(value, str):
                value = value.strip() or None
            setattr(claim, key, value)
            touched.append(key)
        if touched:
            record_event(
                repos.audit,
                firm_id=claim.firm_id,
                client_id=claim.client_id,
                entity_type="claim",
                entity_id=claim.id,
                event_type="edited",
                actor=actor,
                detail={"header_fields": sorted(touched)},
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
        claim = self.get(repos, claim_id)
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
        claim = self.get(repos, claim_id)
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
        claim = self.get(repos, claim_id)
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
                    doc_date=ln.doc_date,
                    amount=ln.total_amount,
                    currency=ln.currency,
                    quantity=ln.quantity,
                    unit=ln.unit,
                    cost_centre=ln.cost_centre_override,
                    direction="forward",
                    idempotency_key=_idempotency_key(claim.client_id, ln.id),
                    carbon_ref=f"CARB-{canonical_hash([self._payload(ln, cat)])[:12].upper()}",
                )
            )
        claim.status = "released"
        StubSink().post(digest, len(relevant))

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
        return batch

    def reverse(
        self, *, repos: "Repos", claim_id: uuid.UUID, actor: str,
        principal: "Principal | None" = None,
    ) -> ReleaseBatch:
        """Correct a released claim by forwarding REVERSAL rows to CarbonNext — one
        per relevant line of the original release, with negated amount/quantity and
        ``direction='reversal'``. Never edits or deletes the originals; a correction
        is a new, opposite-signed handoff batch."""
        claim = self.get(repos, claim_id)
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
                    doc_date=ln.doc_date,
                    amount=None if ln.total_amount is None else -ln.total_amount,
                    currency=ln.currency,
                    quantity=None if ln.quantity is None else -ln.quantity,
                    unit=ln.unit,
                    cost_centre=ln.cost_centre_override,
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
        return batch


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
        )


def _idempotency_key(client_id: uuid.UUID, claim_id: uuid.UUID, suffix: str = "") -> str:
    raw = f"{client_id}{claim_id}{suffix}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
