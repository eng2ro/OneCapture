"""Shared release path for ERP Sync: clean staged rows → the immutable ledger.

Release is the deliberate, audited step that turns reviewed ERP Sync staging rows
into ``emission_entry`` ledger records under one ``release_batch`` — through the
SAME machinery e-Claim uses: :mod:`core.release` (canonical batch hash + stubbed
TSA/Carbon Next seams), :mod:`core.audit` (hash-chained trail), and the shared
``ReleaseBatch`` / ``EmissionEntry`` / ``AuditEvent`` tables + repositories. Both
channels therefore feed one ledger, one audit trail, and (eventually) one FR-S6
anchor — nothing here is ERP-Sync-private except the projection of a staging row.

It is a *distinct, on-demand* step, consistent with stage-then-release: importing
stages rows; this releases them. Pass 1 releases the auto-clean rows
(``status='clean'``); FR-S5 will later route approved held/flagged rows through
this exact function. Idempotent by construction — each released row flips
``clean → released`` as it projects, so a re-release finds nothing and is a no-op
(belt-and-suspenders: the per-line ``idempotency_key`` UNIQUE blocks a double
ledger entry even if a flip were ever missed).

The service never commits — the caller owns the transaction, so the whole release
(batch + every entry + every audit event + the status flips) is one atomic unit.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.release import StubSink, StubTSA, canonical_hash
from eclaim.db.models import EmissionEntry, ErpsyncEntry, ReleaseBatch
from eclaim.repositories import AuditRepository, ReleaseRepository
from eclaim.services.audit import record_event

SOURCE_TYPE = "erpsync"

# erpsync versions its factor *set* with a string ("MY-2026.1"), but the shared
# emission_entry ledger keys factor_version as an int (e-Claim's
# EmissionFactor.version). The exact set version + full rule provenance live on the
# immutable source erpsync_entry row (reachable via source_type/source_id) and are
# bound into the batch hash; this int column carries 0 as a sentinel meaning
# "string-versioned — see the source row" rather than fabricating a fake version.
_LEDGER_FACTOR_VERSION = 0


# Statuses a release projects into the ledger: auto-classified ``clean`` rows and
# human-reviewed ``approved`` rows (FR-S5). Both are "ready"; the distinction is
# provenance, preserved on the row + in the audit chain, not in releasability.
RELEASABLE_STATUSES = ("clean", "approved")


def release_clean(
    session: Session,
    *,
    firm_id: uuid.UUID,
    client_id: uuid.UUID,
    actor: str,
) -> ReleaseBatch | None:
    """Release every releasable (``clean`` or ``approved``) staged row for one
    client into the ledger.

    Returns the created :class:`ReleaseBatch`, or ``None`` when there is nothing
    to release (the idempotent no-op — no empty batch is written).
    """
    releases = ReleaseRepository(session)
    audit = AuditRepository(session)

    rows = list(
        session.execute(
            select(ErpsyncEntry)
            .where(
                ErpsyncEntry.client_id == client_id,
                ErpsyncEntry.status.in_(RELEASABLE_STATUSES),
            )
            .order_by(ErpsyncEntry.created_at, ErpsyncEntry.id)
        ).scalars()
    )
    if not rows:
        return None  # nothing to release — idempotent no-op

    # One deterministic anchor over the whole batch's rich projections (binds the
    # string factor-set version + rule provenance the int ledger column can't hold).
    digest = canonical_hash([_projection(row) for row in rows])
    token = StubTSA().stamp(digest)
    total = sum((row.tco2e for row in rows), start=Decimal("0"))

    # Written in a savepoint. Two concurrent release_clean calls read the same clean
    # rows and compute the same digest; the loser collides on UNIQUE(client_id,
    # batch_hash) on release_batch. Map that collision to the same idempotent no-op the
    # e-Claim release path uses — return the winner's batch, not a 500 (punch-list P7).
    try:
        with session.begin_nested():
            batch = releases.add_batch(
                ReleaseBatch(
                    firm_id=firm_id,
                    client_id=client_id,
                    source_type=SOURCE_TYPE,
                    created_by=actor,
                    batch_hash=digest,
                    tsa_token=token,
                    record_count=len(rows),
                    total_tco2e=total,
                    status="released",
                )
            )

            for row in rows:
                idem = _idempotency_key(client_id, row.id)
                if releases.entry_for(idem) is None:
                    releases.add_entry(
                        EmissionEntry(
                            firm_id=firm_id,
                            client_id=client_id,
                            source_type=SOURCE_TYPE,
                            source_id=row.id,
                            scope=_scope_to_int(row.scope),
                            factor_key=row.factor_ref,
                            factor_version=_LEDGER_FACTOR_VERSION,
                            quantity=row.quantity,
                            unit=row.uom,
                            basis=row.basis,
                            tco2e=row.tco2e,
                            release_batch_id=batch.id,
                            idempotency_key=idem,
                            carbon_ref=f"CARB-{idem[:12].upper()}",
                        )
                    )
                row.status = "released"
                record_event(
                    audit,
                    firm_id=firm_id,
                    client_id=client_id,
                    entity_type="erpsync_entry",
                    entity_id=row.id,
                    event_type="released",
                    actor=actor,
                    detail={"batch_hash": digest, "release_batch_id": str(batch.id)},
                )

            # External seams (stubbed): Carbon Next post + batch-level TSA anchor event.
            StubSink().post(digest, len(rows))
            record_event(
                audit,
                firm_id=firm_id,
                client_id=client_id,
                entity_type="release_batch",
                entity_id=batch.id,
                event_type="tsa_anchored",
                actor="system",
                detail={"tsa_token": token, "record_count": len(rows)},
            )
    except IntegrityError:
        prior = releases.batch_by_hash(client_id, digest)
        if prior is not None:
            return prior          # a concurrent release already anchored this batch
        raise
    session.flush()
    return batch


def _projection(row: ErpsyncEntry) -> dict:
    """Carbon-relevant, hash-stable projection of one staged row.

    Mirrors e-Claim's claim projection but carries ERP Sync's full factor + rule
    provenance (the string factor-set version included), so the batch hash anchors
    exactly what classified the figure even though the ledger column is lossy.
    """
    return {
        "erpsync_entry_id": str(row.id),
        "scope": row.scope,
        "factor_ref": row.factor_ref,
        "factor_version": row.factor_version,
        "rule_id": row.rule_id,
        "rule_version": row.rule_version,
        "quantity": None if row.quantity is None else format(row.quantity, "f"),
        "tco2e": format(row.tco2e, "f"),
        "source_hash": row.source_hash,
    }


def _scope_to_int(scope: str) -> int:
    """ERP Sync string scope → the ledger's GHG smallint (any scope_3_* → 3)."""
    return {"scope_1": 1, "scope_2": 2}.get(scope, 3)


def _idempotency_key(client_id: uuid.UUID, entry_id: uuid.UUID) -> str:
    raw = f"{client_id}{entry_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
