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

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from .db.models import (
    AuditEvent,
    Category,
    Claim,
    Claimant,
    EmissionEntry,
    EmissionFactor,
    ReleaseBatch,
)
from .services.classify import FactorView


class FactorRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_active(self, factor_key: str) -> FactorView | None:
        """Highest-version active factor for a key, or None."""
        row = self._s.execute(
            select(EmissionFactor)
            .where(EmissionFactor.factor_key == factor_key, EmissionFactor.active.is_(True))
            .order_by(EmissionFactor.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return FactorView(
            factor_key=row.factor_key,
            version=row.version,
            scope=row.scope,
            unit=row.unit,
            factor_kg_per_unit=row.factor_kg_per_unit,
        )


class CategoryRepository:
    """The per-client expense_type → factor mapping master. RLS scopes reads to
    the caller's firm/clients exactly like the other data tables."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, client_id: uuid.UUID, expense_type: str) -> Category | None:
        """The category for one client + OCR expense_type, or None (unmapped)."""
        return self._s.execute(
            select(Category).where(
                Category.client_id == client_id,
                Category.expense_type == expense_type,
            )
        ).scalar_one_or_none()

    def get_by_id(self, category_id: uuid.UUID) -> Category | None:
        return self._s.get(Category, category_id)


class ClaimRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, claim: Claim) -> Claim:
        self._s.add(claim)
        self._s.flush()
        return claim

    def get(self, claim_id: uuid.UUID) -> Claim | None:
        return self._s.get(Claim, claim_id)

    def list(self, client_id: uuid.UUID, status: str | None = None) -> list[Claim]:
        stmt = select(Claim).where(Claim.client_id == client_id)
        if status is not None:
            stmt = stmt.where(Claim.status == status)
        stmt = stmt.order_by(Claim.created_at.desc())
        return list(self._s.execute(stmt).scalars())

    def export_rows(
        self,
        *,
        client_id: uuid.UUID | None = None,
        status: str | None = "released",
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        batch_id: uuid.UUID | None = None,
    ):
        """Read-only join for the accounting CSV export — one row per matching
        claim: claim → claimant (name/employee_ref/cost_centre) and claim →
        category (name/gl_export_code), with the release_batch_id of the claim's
        original ledger entry. RLS scopes every table to the caller's firm/clients.

        Columns are emitted in CSV order. ``date_from``/``date_to`` filter on
        ``created_at`` (a real timestamptz; ``doc_date`` is free text). A reversed
        claim has a second negative entry — the scalar subquery takes the earliest
        (original release) batch, so the claim stays a single row."""
        ee = EmissionEntry
        batch_col = (
            select(ee.release_batch_id)
            .where(ee.source_type == "eclaim", ee.source_id == Claim.id)
            .order_by(ee.created_at)
            .limit(1)
            .correlate(Claim)
            .scalar_subquery()
        )
        stmt = (
            select(
                Claim.id,
                Claim.doc_date,
                Claim.status,
                Claimant.name.label("claimant_name"),
                Claimant.employee_ref,
                Claimant.cost_centre,
                Claim.vendor,
                Claim.doc_no,
                Category.name.label("category_name"),
                Category.gl_export_code,
                Claim.currency,
                Claim.total_amount,
                Claim.scope,
                Claim.basis,
                Claim.tco2e,
                Claim.factor_key,
                batch_col.label("release_batch_id"),
            )
            .select_from(Claim)
            .outerjoin(Claimant, Claim.submitted_by_claimant_id == Claimant.id)
            .outerjoin(Category, Claim.category_id == Category.id)
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
                    ee.source_type == "eclaim",
                    ee.source_id == Claim.id,
                    ee.release_batch_id == batch_id,
                )
            )
        return self._s.execute(stmt.order_by(Claim.created_at)).all()


class ReleaseRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add_batch(self, batch: ReleaseBatch) -> ReleaseBatch:
        self._s.add(batch)
        self._s.flush()
        return batch

    def entry_for(self, idempotency_key: str) -> EmissionEntry | None:
        return self._s.execute(
            select(EmissionEntry).where(EmissionEntry.idempotency_key == idempotency_key)
        ).scalar_one_or_none()

    def add_entry(self, entry: EmissionEntry) -> EmissionEntry:
        self._s.add(entry)
        self._s.flush()
        return entry


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
    def __init__(self, session: Session) -> None:
        self._s = session

    def entries(self, client_id: uuid.UUID) -> list[EmissionEntry]:
        return list(
            self._s.execute(
                select(EmissionEntry)
                .where(EmissionEntry.client_id == client_id)
                .order_by(EmissionEntry.created_at)
            ).scalars()
        )

    def scope_totals(self, client_id: uuid.UUID) -> dict[int, Decimal]:
        """tCO2e summed per scope, computed in SQL."""
        from sqlalchemy import func

        rows = self._s.execute(
            select(EmissionEntry.scope, func.coalesce(func.sum(EmissionEntry.tco2e), 0))
            .where(EmissionEntry.client_id == client_id)
            .group_by(EmissionEntry.scope)
        ).all()
        return {int(scope): Decimal(total) for scope, total in rows}
