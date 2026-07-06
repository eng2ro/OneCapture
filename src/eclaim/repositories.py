"""Repositories — the persistence seam between services and Postgres.

Each repository wraps a SQLAlchemy :class:`Session`. Services depend on these,
never on the ORM directly, so a different backend (or multi-tenant scoping)
slots in here. Transaction control is the caller's: repositories add/flush but
do not commit, so a whole operation commits or rolls back as one unit.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import exists, func, select, text
from sqlalchemy.orm import Session

from .db.models import (
    ApprovalMatrixRule,
    AuditEvent,
    CarbonHandoff,
    Category,
    Claim,
    Claimant,
    ClaimLine,
    EmissionEntry,
    Event,
    ReleaseBatch,
)


class CategoryRepository:
    """The per-client expense_type → factor mapping master. RLS scopes reads to
    the caller's firm/clients exactly like the other data tables."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def match_single(self, client_id: uuid.UUID, expense_type: str) -> Category | None:
        """Auto-match an OCR ``expense_type`` to a category ONLY when unambiguous:
        exactly one active category maps that type. Since 0007 a client may have
        several categories sharing one carbon ``expense_type`` (e.g. many
        spend-based 'other' categories), so zero *or* more-than-one match returns
        None → the claim is left unmapped for a reviewer/claimant to pick the
        right category. Used by channels that don't choose a category explicitly
        (API/email); the web capture form posts ``category_id`` instead."""
        rows = list(
            self._s.execute(
                select(Category).where(
                    Category.client_id == client_id,
                    Category.expense_type == expense_type,
                    Category.status == "active",
                )
            ).scalars()
        )
        return rows[0] if len(rows) == 1 else None

    def match_by_merchant(
        self, client_id: uuid.UUID, vendor: str | None, ocr_expense_type: str | None
    ) -> Category | None:
        """Auto-suggest a category using the merchant name when the OCR type is
        ambiguous. Precedence:

        1. If OCR gave a SPECIFIC type (not 'other') that maps to exactly one
           category, trust it — OCR's fuel_diesel/electricity/air_travel is more
           precise than a merchant guess.
        2. Otherwise (OCR='other' or its type has no/many categories), map the
           merchant name to a category slug (McDonald's -> meals, Grab -> taxi,
           Shell -> fuel) and match that.
        3. Fall back to the OCR type as-is (usually None for 'other').

        Returns None only when nothing resolves — the line stays unmapped for
        review, never wrong-guessed."""
        from .services.merchant import merchant_slug

        et = (ocr_expense_type or "other").strip() or "other"
        if et != "other":
            cat = self.match_single(client_id, et)
            if cat is not None:
                return cat
        slug = merchant_slug(vendor)
        if slug is not None:
            cat = self.match_single(client_id, slug)
            if cat is not None:
                return cat
        return self.match_single(client_id, et)

    def get_by_id(self, category_id: uuid.UUID) -> Category | None:
        return self._s.get(Category, category_id)

    def list_for_client(self, client_id: uuid.UUID) -> list[Category]:
        """Active categories for one client, for the review-page assign dropdown."""
        return list(
            self._s.execute(
                select(Category)
                .where(Category.client_id == client_id, Category.status == "active")
                .order_by(Category.name)
            ).scalars()
        )

    def list_for_clients(self, client_ids) -> list[Category]:
        """All categories across a set of clients (the admin screen), RLS-scoped."""
        client_ids = list(client_ids)
        if not client_ids:
            return []
        return list(
            self._s.execute(
                select(Category)
                .where(Category.client_id.in_(client_ids))
                .order_by(Category.client_id, Category.name)
            ).scalars()
        )


class ApprovalMatrixRepository:
    """Approval authority matrix (Appendix B). RLS-scoped like the other data
    tables; the explicit client filter is belt-and-suspenders on top."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def rules_for_client(self, client_id: uuid.UUID) -> list[ApprovalMatrixRule]:
        """Active rules for one client (the approval engine reads these). Ordered by
        step then band floor so a caller can walk them predictably."""
        return list(
            self._s.execute(
                select(ApprovalMatrixRule)
                .where(
                    ApprovalMatrixRule.client_id == client_id,
                    ApprovalMatrixRule.active.is_(True),
                )
                .order_by(ApprovalMatrixRule.step_order, ApprovalMatrixRule.min_amount)
            ).scalars()
        )

    def list_for_clients(self, client_ids) -> list[ApprovalMatrixRule]:
        """All rules (active or not) across a set of clients — the admin editor."""
        client_ids = list(client_ids)
        if not client_ids:
            return []
        return list(
            self._s.execute(
                select(ApprovalMatrixRule)
                .where(ApprovalMatrixRule.client_id.in_(client_ids))
                .order_by(
                    ApprovalMatrixRule.client_id,
                    ApprovalMatrixRule.step_order,
                    ApprovalMatrixRule.min_amount,
                )
            ).scalars()
        )

    def get(self, rule_id: uuid.UUID) -> ApprovalMatrixRule | None:
        return self._s.get(ApprovalMatrixRule, rule_id)

    def add(self, rule: ApprovalMatrixRule) -> ApprovalMatrixRule:
        self._s.add(rule)
        self._s.flush()
        return rule

    def delete_for_client(self, client_id: uuid.UUID) -> None:
        """Clear a client's rules — the admin editor rewrites the whole set (the
        wizard/template pattern writes a fresh row-set)."""
        for rule in self.list_for_clients([client_id]):
            self._s.delete(rule)
        self._s.flush()


class ClaimantRepository:
    """Claimant master (submitters, channel-bound — no login). RLS-scoped."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_id(self, claimant_id: uuid.UUID) -> Claimant | None:
        return self._s.get(Claimant, claimant_id)

    def list_for_clients(self, client_ids) -> list[Claimant]:
        client_ids = list(client_ids)
        if not client_ids:
            return []
        return list(
            self._s.execute(
                select(Claimant)
                .where(Claimant.client_id.in_(client_ids))
                .order_by(Claimant.client_id, Claimant.name)
            ).scalars()
        )


class ClaimRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, claim: Claim) -> Claim:
        self._s.add(claim)
        self._s.flush()
        return claim

    def get(self, claim_id: uuid.UUID) -> Claim | None:
        return self._s.get(Claim, claim_id)

    def lock_for_update(self, claim_id: uuid.UUID) -> Claim | None:
        """Load the claim row with ``SELECT … FOR UPDATE`` so concurrent lifecycle
        transitions (approve/decide/release/reverse) on the same claim serialise: a
        second request blocks until the first commits, then reads the committed state
        and takes the idempotent path instead of double-writing."""
        return self._s.execute(
            select(Claim).where(Claim.id == claim_id).with_for_update()
        ).scalar_one_or_none()

    # -- lines (the per-receipt records under a claim header) --------------- #
    def add_line(self, line: ClaimLine) -> ClaimLine:
        self._s.add(line)
        self._s.flush()
        return line

    def delete_line(self, line: ClaimLine) -> None:
        """Remove a line (used when merging lines into one). Totals are re-rolled by
        the caller."""
        self._s.delete(line)
        self._s.flush()

    def lines(self, claim_id: uuid.UUID) -> list[ClaimLine]:
        return list(
            self._s.execute(
                select(ClaimLine)
                .where(ClaimLine.claim_id == claim_id)
                .order_by(ClaimLine.line_no)
            ).scalars()
        )

    def line(self, line_id: uuid.UUID) -> ClaimLine | None:
        return self._s.get(ClaimLine, line_id)

    def first_line(self, claim_id: uuid.UUID) -> ClaimLine | None:
        return self._s.execute(
            select(ClaimLine)
            .where(ClaimLine.claim_id == claim_id)
            .order_by(ClaimLine.line_no)
            .limit(1)
        ).scalar_one_or_none()

    def next_line_no(self, claim_id: uuid.UUID) -> int:
        mx = self._s.execute(
            select(func.max(ClaimLine.line_no)).where(ClaimLine.claim_id == claim_id)
        ).scalar()
        return (mx or 0) + 1

    def next_claim_no(self, *, year: int) -> str:
        """Allocate the next human-readable claim reference, ``CLM-<year>-<NNNNNN>``,
        from the atomic ``claim_no_seq`` sequence (migration 0016)."""
        seq = self._s.execute(text("SELECT nextval('claim_no_seq')")).scalar_one()
        return f"CLM-{year}-{int(seq):06d}"

    def lines_by_claim(self, claim_ids) -> dict[uuid.UUID, list[ClaimLine]]:
        """All lines for a set of claims, grouped by claim_id — one query for the
        inbox/review carbon chips so the page doesn't N+1."""
        claim_ids = list(claim_ids)
        if not claim_ids:
            return {}
        out: dict[uuid.UUID, list[ClaimLine]] = {}
        for ln in self._s.execute(
            select(ClaimLine).where(ClaimLine.claim_id.in_(claim_ids)).order_by(ClaimLine.line_no)
        ).scalars():
            out.setdefault(ln.claim_id, []).append(ln)
        return out

    def list(self, client_id: uuid.UUID, status: str | None = None) -> list[Claim]:
        stmt = select(Claim).where(Claim.client_id == client_id)
        if status is not None:
            stmt = stmt.where(Claim.status == status)
        stmt = stmt.order_by(Claim.created_at.desc())
        return list(self._s.execute(stmt).scalars())

    def list_for_clients(
        self, client_ids, status=None
    ) -> list[Claim]:
        """Claims across a set of clients (the principal's visible clients), newest
        first, optionally filtered by status. ``status`` may be a single status
        string or a list/tuple/set of statuses (a sidebar filter group, e.g. "needs
        attention" = sent_back + partially_approved). Belt-and-suspenders to RLS: the
        explicit client filter keeps the cut correct even on an owner connection."""
        client_ids = list(client_ids)
        if not client_ids:
            return []
        stmt = select(Claim).where(Claim.client_id.in_(client_ids))
        if status is not None:
            if isinstance(status, (list, tuple, set)):
                stmt = stmt.where(Claim.status.in_(list(status)))
            else:
                stmt = stmt.where(Claim.status == status)
        return list(self._s.execute(stmt.order_by(Claim.created_at.desc())).scalars())

    def status_counts(self, client_ids) -> dict[str, int]:
        """Per-status claim counts across the principal's visible clients — feeds
        the sidebar badges and topbar summary. One GROUP BY, RLS-scoped like the
        listings, with the same explicit client filter as belt-and-suspenders."""
        client_ids = list(client_ids)
        if not client_ids:
            return {}
        rows = self._s.execute(
            select(Claim.status, func.count())
            .where(Claim.client_id.in_(client_ids))
            .group_by(Claim.status)
        ).all()
        return {status: count for status, count in rows}

    def inbox_summary(self, client_ids) -> dict:
        """Aggregate figures for the inbox KPI strip — total claim count, summed
        claimed amount (header total_claimed), and the number of carbon-relevant
        claims (≥1 line flagged carbon_relevant) — in one pass."""
        client_ids = list(client_ids)
        if not client_ids:
            return {"total": 0, "total_amount": Decimal("0"), "carbon_count": 0}
        carbon_line = (
            exists()
            .where(
                ClaimLine.claim_id == Claim.id,
                ClaimLine.carbon_relevant.is_(True),
            )
        )
        total, total_amount, carbon_count = self._s.execute(
            select(
                func.count(),
                func.coalesce(func.sum(Claim.total_claimed), 0),
                func.count().filter(carbon_line),
            ).where(Claim.client_id.in_(client_ids))
        ).one()
        return {
            "total": total,
            "total_amount": total_amount,
            "carbon_count": carbon_count,
        }

    def export_rows(
        self,
        *,
        client_id: uuid.UUID | None = None,
        status: str | None = "released",
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        batch_id: uuid.UUID | None = None,
    ):
        """Read-only join for the accounting / ERP reimbursement export — one row
        per line: claim_line → claim → claimant → category, with the
        release_batch_id of the line's ledger entry and its ``line_status`` (so the
        consumer can take approved lines only). ALL lines export (carbon and
        non-carbon alike) — the carbon split happens on the Carbon Next side, not
        here. RLS scopes every table to the caller's firm/clients.

        ``status`` filters the CLAIM status (default 'released'). ``date_from``/
        ``date_to`` filter the claim ``created_at``. The scalar subquery takes the
        line's earliest (original forward) handoff batch, so a reversed line stays
        one row. Non-carbon lines have no handoff → NULL batch (they still export)."""
        ch = CarbonHandoff
        batch_col = (
            select(ch.release_batch_id)
            .where(ch.line_id == ClaimLine.id, ch.direction == "forward")
            .order_by(ch.created_at)
            .limit(1)
            .correlate(ClaimLine)
            .scalar_subquery()
        )
        stmt = (
            select(
                Claim.id.label("claim_id"),
                ClaimLine.line_no,
                ClaimLine.doc_date,
                Claim.status.label("claim_status"),
                ClaimLine.line_status,
                Claimant.name.label("claimant_name"),
                Claimant.employee_ref,
                # Resolved posting dimensions: a line override wins over the
                # claimant default, then the event's cost centre — the SAME order
                # the release posting-gate (_resolved_cost_centre) checks, so a line
                # that passed the gate via the event never exports a blank cost centre.
                func.coalesce(
                    ClaimLine.cost_centre_override, Claimant.cost_centre, Event.cost_centre
                ).label("cost_centre"),
                ClaimLine.vendor,
                ClaimLine.doc_no,
                Category.name.label("category_name"),
                func.coalesce(ClaimLine.gl_code, Category.gl_export_code).label("gl_code"),
                ClaimLine.payment_method,
                ClaimLine.reimbursable,
                ClaimLine.currency,
                ClaimLine.total_amount,
                ClaimLine.tax_amount,
                ClaimLine.tax_code,
                ClaimLine.net_amount,
                ClaimLine.fx_rate,
                ClaimLine.base_amount,
                ClaimLine.posting_date,
                ClaimLine.department,
                ClaimLine.project_code,
                ClaimLine.supplier_tax_id,
                ClaimLine.carbon_relevant,
                batch_col.label("release_batch_id"),
            )
            .select_from(ClaimLine)
            .join(Claim, ClaimLine.claim_id == Claim.id)
            .outerjoin(Claimant, Claim.submitted_by_claimant_id == Claimant.id)
            .outerjoin(Category, ClaimLine.category_id == Category.id)
            .outerjoin(Event, Claim.event_id == Event.id)
        )
        if status is not None:
            stmt = stmt.where(Claim.status == status)
        if client_id is not None:
            stmt = stmt.where(Claim.client_id == client_id)
        if date_from is not None:
            stmt = stmt.where(Claim.created_at >= date_from)
        if date_to is not None:
            stmt = stmt.where(Claim.created_at <= date_to)
        if batch_id is not None:
            stmt = stmt.where(
                exists().where(
                    ch.line_id == ClaimLine.id,
                    ch.release_batch_id == batch_id,
                )
            )
        return self._s.execute(stmt.order_by(Claim.created_at, ClaimLine.line_no)).all()


class ReleaseRepository:
    """Release batches + the shared ``emission_entry`` ledger.

    e-Claim no longer writes ``emission_entry`` (its per-line payload lives in
    ``carbon_handoff``; see :class:`CarbonHandoffRepository`). But **ERP Sync** DOES
    — it computes real tonnage and writes ``emission_entry`` via ``add_entry`` /
    ``entry_for`` / ``entry_for_sources`` here — so these stay for ERP Sync."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add_batch(self, batch: ReleaseBatch) -> ReleaseBatch:
        self._s.add(batch)
        self._s.flush()
        return batch

    def batch_by_hash(self, client_id: uuid.UUID, batch_hash: str) -> ReleaseBatch | None:
        """Find a release batch by its (deterministic) content hash — used to make
        release idempotent for a claim that produced NO carbon handoffs to key on."""
        return self._s.execute(
            select(ReleaseBatch).where(
                ReleaseBatch.client_id == client_id,
                ReleaseBatch.batch_hash == batch_hash,
            )
        ).scalars().first()

    def entry_for(self, idempotency_key: str) -> EmissionEntry | None:
        return self._s.execute(
            select(EmissionEntry).where(EmissionEntry.idempotency_key == idempotency_key)
        ).scalar_one_or_none()

    def entry_for_sources(self, source_ids) -> EmissionEntry | None:
        """Earliest ledger entry for any of these source ids — used by ERP Sync to
        detect an already-released line and find its original batch."""
        source_ids = list(source_ids)
        if not source_ids:
            return None
        return self._s.execute(
            select(EmissionEntry)
            .where(EmissionEntry.source_id.in_(source_ids))
            .order_by(EmissionEntry.created_at)
            .limit(1)
        ).scalar_one_or_none()

    def add_entry(self, entry: EmissionEntry) -> EmissionEntry:
        self._s.add(entry)
        self._s.flush()
        return entry


class CarbonHandoffRepository:
    """e-Claim -> CarbonNext raw handoff log. One row per forwarded (or reversed)
    carbon-relevant line. RLS scopes reads to the caller's firm/clients."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, handoff: CarbonHandoff) -> CarbonHandoff:
        self._s.add(handoff)
        self._s.flush()
        return handoff

    def by_idempotency(self, key: str) -> CarbonHandoff | None:
        return self._s.execute(
            select(CarbonHandoff).where(CarbonHandoff.idempotency_key == key)
        ).scalar_one_or_none()

    def first_for_lines(self, line_ids) -> CarbonHandoff | None:
        """Earliest FORWARD handoff for any of these line ids — used to detect an
        already-released claim and return its original batch (idempotency)."""
        line_ids = list(line_ids)
        if not line_ids:
            return None
        return self._s.execute(
            select(CarbonHandoff)
            .where(
                CarbonHandoff.line_id.in_(line_ids),
                CarbonHandoff.direction == "forward",
            )
            .order_by(CarbonHandoff.created_at)
            .limit(1)
        ).scalar_one_or_none()


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def tip_hash(self, entity_type: str, entity_id: uuid.UUID) -> str | None:
        """Hash of the chain tip — the event no later event points back to.

        Identifying the tip structurally (rather than by timestamp) is robust to
        Postgres ``now()`` returning the same value for every row written in one
        transaction.
        """
        later = AuditEvent.__table__.alias("ae_later")
        has_successor = exists().where(
            later.c.entity_type == entity_type,
            later.c.entity_id == entity_id,
            later.c.prev_hash == AuditEvent.hash,
        )
        stmt = (
            select(AuditEvent.hash)
            .where(
                AuditEvent.entity_type == entity_type,
                AuditEvent.entity_id == entity_id,
                ~has_successor,
            )
            .limit(1)
        )
        return self._s.execute(stmt).scalar_one_or_none()

    def add(self, event: AuditEvent) -> AuditEvent:
        self._s.add(event)
        self._s.flush()
        return event

    def chain(self, entity_type: str, entity_id: uuid.UUID) -> list[AuditEvent]:
        """Events for an entity in chain order (genesis → tip).

        Ordered by following ``prev_hash → hash`` links rather than timestamps,
        so same-transaction ``now()`` ties never scramble the order.
        """
        events = list(
            self._s.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == entity_type,
                    AuditEvent.entity_id == entity_id,
                )
            ).scalars()
        )
        by_prev = {(e.prev_hash or ""): e for e in events}
        ordered: list[AuditEvent] = []
        cursor = ""
        while cursor in by_prev:
            nxt = by_prev[cursor]
            ordered.append(nxt)
            cursor = nxt.hash
        # Fall back to insertion-ish order if the chain can't be fully linked.
        return ordered if len(ordered) == len(events) else events


class LedgerRepository:
    """The CarbonNext handoff log (post-0011). e-Claim does no carbon maths — this
    is the record of which lines were FORWARDED to CarbonNext with what raw data."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def entries(self, client_id: uuid.UUID) -> list[CarbonHandoff]:
        return list(
            self._s.execute(
                select(CarbonHandoff)
                .where(CarbonHandoff.client_id == client_id)
                .order_by(CarbonHandoff.created_at)
            ).scalars()
        )

    def direction_counts(self, client_id: uuid.UUID) -> dict[str, int]:
        """How many lines forwarded vs reversed — the handoff KPI (e-Claim forwards
        raw data; CarbonNext owns scope/factor/tonnage, so there is no scope split
        here)."""
        rows = self._s.execute(
            select(CarbonHandoff.direction, func.count())
            .where(CarbonHandoff.client_id == client_id)
            .group_by(CarbonHandoff.direction)
        ).all()
        return {direction: int(n) for direction, n in rows}


class EventRepository:
    """Event grouping (trips / trainings) above claims — holds the budget and the
    cross-claim rollup. RLS scopes reads to the caller's firm/clients."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, event: Event) -> Event:
        self._s.add(event)
        self._s.flush()
        return event

    def get(self, event_id: uuid.UUID) -> Event | None:
        return self._s.get(Event, event_id)

    def list_for_clients(self, client_ids) -> list[Event]:
        client_ids = list(client_ids)
        if not client_ids:
            return []
        return list(
            self._s.execute(
                select(Event)
                .where(Event.client_id.in_(client_ids))
                .order_by(Event.created_at.desc())
            ).scalars()
        )

    def spent(self, event_id: uuid.UUID) -> Decimal:
        """Total claimed across every claim tied to this event (across all people)
        — the figure the budget bar compares against ``budget_amount``."""
        return self._s.execute(
            select(func.coalesce(func.sum(Claim.total_claimed), 0)).where(
                Claim.event_id == event_id
            )
        ).scalar_one()

    def claims(self, event_id: uuid.UUID) -> list[Claim]:
        return list(
            self._s.execute(
                select(Claim).where(Claim.event_id == event_id).order_by(Claim.created_at)
            ).scalars()
        )
