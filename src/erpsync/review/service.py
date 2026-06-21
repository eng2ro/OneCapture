"""ERP Sync review service (FR-S5): the held/flagged queue's actions.

A reviewer works the rows the import set aside — cross-channel ``held`` dedup
holds and ``flagged`` (unmapped / spend-based / DQ) rows — and decides each one's
fate. Approved rows flip to ``approved`` and are picked up by the SAME
:func:`erpsync.release.service.release_clean` path as the auto-``clean`` rows
(its filter spans ``clean`` + ``approved``); dismissed rows go terminal.

Actions and their transitions:

* **approve** (``held`` → approve-as-distinct, or ``flagged`` → accept-as-is):
  ``→ approved``, records the reviewer.
* **remap** (``flagged`` only): re-map category/scope/factor and recompute tCO2e
  via the shared :func:`core.carbon.tco2e`; records the *editor* (maker) but does
  NOT change status — a separate approve (by a different user, per SoD) releases it.
* **dismiss** (``held`` reject-as-duplicate, or ``flagged`` dismiss): ``→ dismissed``
  (terminal), with the intent in the audit ``event_type`` + a ``review_note``.

Every mutating action runs the SoD guard under the live principal (no viewers, must
hold the client grant, within authority limit, and maker≠checker) — defence in
depth with the ``ck_erpsync_entry_sod`` DB CHECK. The service never commits; the
caller (API route) owns the transaction.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.carbon import tco2e as compute_tco2e
from eclaim.auth.principal import Principal
from eclaim.db.models import ErpsyncEntry
from eclaim.repositories import AuditRepository
from eclaim.services.audit import record_event

# The two statuses a reviewer may act on.
REVIEWABLE = ("held", "flagged")

ENTITY_TYPE = "erpsync_entry"


class ReviewError(RuntimeError):
    """Base for review-service errors (mapped to 4xx by the API)."""


class EntryNotFound(ReviewError):
    pass


class IllegalReviewState(ReviewError):
    """An action not allowed from the row's current status (→ 409)."""


class ReviewSoDViolation(ReviewError):
    """A review that violates separation of duties / authority (→ 403)."""


@dataclass(frozen=True)
class RemapInput:
    """A reviewer's re-mapping of a flagged line. ``factor_value`` is the kgCO2e
    per unit for the chosen factor (the UI populates it from the client's factor
    set); tCO2e is recomputed from it, never trusted from the client."""

    category: str
    scope: str
    basis: str
    factor_ref: str
    factor_value: Decimal
    quantity: Decimal | None = None
    uom: str | None = None
    amount: Decimal | None = None


# --------------------------------------------------------------------------- #
# SoD guard (under the live principal)
# --------------------------------------------------------------------------- #
def check_can_review(entry: ErpsyncEntry, reviewer: Principal) -> None:
    """Raise :class:`ReviewSoDViolation` if ``reviewer`` may not review ``entry``.

    Reuses the spine's SoD dimensions: no viewers, must hold the client grant,
    within the authority limit (on the line amount), and the maker-checker rule —
    the user who remapped/edited a row cannot be the one who approves/dismisses it.
    """
    if reviewer.base_role == "viewer":
        raise ReviewSoDViolation("viewers cannot review entries")
    if not reviewer.can_access_client(entry.client_id):
        raise ReviewSoDViolation("reviewer has no grant to this client")
    if entry.edited_by_user_id is not None and entry.edited_by_user_id == reviewer.user_id:
        raise ReviewSoDViolation("the user who edited a row cannot review it")
    amount = entry.amount
    if (
        amount is not None
        and reviewer.authority_limit is not None
        and amount > reviewer.authority_limit
    ):
        raise ReviewSoDViolation(
            f"amount {amount} exceeds reviewer authority limit {reviewer.authority_limit}"
        )


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def get_entry(session: Session, entry_id: uuid.UUID) -> ErpsyncEntry:
    entry = session.get(ErpsyncEntry, entry_id)
    if entry is None:
        raise EntryNotFound(str(entry_id))
    return entry


def review_queue(
    session: Session, client_ids: frozenset[uuid.UUID]
) -> list[ErpsyncEntry]:
    """Held + flagged rows awaiting review, restricted to the principal's visible
    clients (app-layer narrowing on top of RLS). Empty ``client_ids`` → no rows."""
    if not client_ids:
        return []
    return list(
        session.execute(
            select(ErpsyncEntry)
            .where(
                ErpsyncEntry.client_id.in_(client_ids),
                ErpsyncEntry.status.in_(REVIEWABLE),
            )
            .order_by(ErpsyncEntry.client_id, ErpsyncEntry.status, ErpsyncEntry.created_at)
        ).scalars()
    )


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def approve(
    session: Session,
    *,
    entry_id: uuid.UUID,
    reviewer: Principal,
    note: str | None = None,
) -> ErpsyncEntry:
    """Approve a held/flagged row → ``approved`` (released later by release_clean)."""
    entry = get_entry(session, entry_id)
    _require_reviewable(entry)
    check_can_review(entry, reviewer)

    prior = entry.status
    entry.status = "approved"
    entry.reviewed_by_user_id = reviewer.user_id
    entry.reviewed_at = _now()
    if note is not None:
        entry.review_note = note
    _audit(session, entry, "approved", reviewer, {"from_status": prior, "note": note})
    session.flush()
    return entry


def remap(
    session: Session,
    *,
    entry_id: uuid.UUID,
    mapping: RemapInput,
    reviewer: Principal,
) -> ErpsyncEntry:
    """Re-map a flagged row and recompute tCO2e; records the editor (maker).

    Stays ``flagged`` — a separate approve (by a different user, per the SoD
    maker-checker rule) is what releases it.
    """
    entry = get_entry(session, entry_id)
    if entry.status != "flagged":
        raise IllegalReviewState(f"cannot remap a row in status {entry.status!r}")
    # A viewer/ungranted user can't edit either; reuse the guard (maker-checker is
    # vacuous here — editing sets the maker, it doesn't check against it).
    check_can_review(entry, reviewer)

    units, data_quality = _recompute_basis(mapping)
    entry.category = mapping.category
    entry.scope = mapping.scope
    entry.basis = mapping.basis
    entry.factor_ref = mapping.factor_ref
    entry.factor_value = mapping.factor_value
    entry.quantity = mapping.quantity
    entry.uom = mapping.uom
    if mapping.amount is not None:
        entry.amount = mapping.amount
    entry.data_quality = data_quality
    entry.tco2e = compute_tco2e(units, mapping.factor_value)
    entry.edited_by_user_id = reviewer.user_id

    _audit(
        session,
        entry,
        "remapped",
        reviewer,
        {
            "factor_ref": mapping.factor_ref,
            "scope": mapping.scope,
            "tco2e": format(entry.tco2e, "f"),
        },
    )
    session.flush()
    return entry


def dismiss(
    session: Session,
    *,
    entry_id: uuid.UUID,
    reviewer: Principal,
    event_type: str = "dismissed",
    note: str | None = None,
) -> ErpsyncEntry:
    """Dismiss a held/flagged row → ``dismissed`` (terminal, never released).

    ``event_type`` carries the intent in the audit trail — ``rejected_duplicate``
    for confirming a dedup hold, ``dismissed`` for out-of-scope.
    """
    entry = get_entry(session, entry_id)
    _require_reviewable(entry)
    check_can_review(entry, reviewer)

    prior = entry.status
    entry.status = "dismissed"
    entry.reviewed_by_user_id = reviewer.user_id
    entry.reviewed_at = _now()
    entry.review_note = note
    _audit(session, entry, event_type, reviewer, {"from_status": prior, "note": note})
    session.flush()
    return entry


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _require_reviewable(entry: ErpsyncEntry) -> None:
    if entry.status not in REVIEWABLE:
        raise IllegalReviewState(
            f"cannot review a row in status {entry.status!r} (only held/flagged)"
        )


def _recompute_basis(mapping: RemapInput) -> tuple[Decimal, str]:
    """The units that drive tCO2e + the resulting data quality for a re-mapping."""
    if mapping.basis == "activity":
        if mapping.quantity is None:
            raise ReviewError("activity basis requires a quantity")
        return mapping.quantity, "measured"
    if mapping.amount is None:
        raise ReviewError("spend basis requires an amount")
    return mapping.amount, "estimated"  # spend-based is always an estimate


def _audit(
    session: Session,
    entry: ErpsyncEntry,
    event_type: str,
    reviewer: Principal,
    detail: dict,
) -> None:
    record_event(
        AuditRepository(session),
        firm_id=entry.firm_id,
        client_id=entry.client_id,
        entity_type=ENTITY_TYPE,
        entity_id=entry.id,
        event_type=event_type,
        actor=reviewer.email or str(reviewer.user_id),
        detail=detail,
    )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
