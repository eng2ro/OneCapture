"""Postgres staging sink: the pipeline's per-line output → ``erpsync_entry``.

This is the multi-tenant counterpart to the JSON :class:`~erpsync.persistence.
store.Store`. Where ``Store`` is the lightweight, dependency-free idempotency
ledger used by the CLI and the pure-Python unit tests, ``PgStagingStore`` lands
EVERY accepted line into the rich, RLS-gated ``erpsync_entry`` table carrying a
review ``status`` — the table 0004 created. It is to ERP Sync what the ``claim``
table is to e-Claim: the reviewable staging row a later release (FR-S5+) projects
into the shared ``emission_entry`` ledger. Clean / held / flagged rows all live
here, distinguished by ``status`` — there is deliberately no separate held table.

Tenancy is supplied by the caller (the real DB ``firm_id`` / ``client_id`` UUIDs),
NOT the pipeline's string ``client_id`` — the pipeline stays storage-agnostic and
this sink stamps the tenant. Idempotency is the database's job: an ``INSERT ...
ON CONFLICT (client_id, doc_entry, line_num) DO NOTHING`` makes a re-import of the
same lines a no-op, so ``stage`` returns the count of rows actually inserted.

SQLAlchemy and the shared ORM model are imported lazily so importing the ERP Sync
core (``erpsync.pipeline``) never drags in the e-Claim DB stack — only callers
that actually wire up a Postgres sink pay for it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from ..domain.models import EmissionEntry

# A staged row: the carbon entry plus its review status (clean / held / flagged).
StagedRow = tuple[EmissionEntry, str]


class PgStagingStore:
    """Stages accepted ERP Sync lines into ``erpsync_entry`` for one tenant.

    Construct with an open SQLAlchemy ``Session`` already carrying the right
    tenant context (the RLS ``WITH CHECK`` policy requires ``app.current_firm`` /
    ``app.allowed_clients`` to match the ``firm_id`` / ``client_id`` stamped here,
    exactly as for every other data table). The session is flushed but NOT
    committed — the caller owns the transaction boundary.
    """

    def __init__(self, session, firm_id: uuid.UUID, client_id: uuid.UUID) -> None:
        self._session = session
        self._firm_id = firm_id
        self._client_id = client_id

    def stage(self, rows: Iterable[StagedRow]) -> int:
        """Insert each (entry, status) row, skipping any whose
        (client_id, doc_entry, line_num) grain is already staged.

        Returns the number of rows actually inserted (new grains), so a re-import
        of an already-staged batch returns 0.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from eclaim.db.models import ErpsyncEntry

        values = [self._to_values(entry, status) for entry, status in rows]
        if not values:
            return 0

        stmt = (
            pg_insert(ErpsyncEntry)
            .values(values)
            .on_conflict_do_nothing(constraint="uq_erpsync_entry_line")
            .returning(ErpsyncEntry.id)
        )
        # RETURNING under ON CONFLICT DO NOTHING yields a row only for grains the
        # INSERT actually wrote, so the count is exact (rowcount is unreliable for a
        # multi-row upsert — psycopg reports -1).
        return len(self._session.execute(stmt).fetchall())

    def _to_values(self, entry: EmissionEntry, status: str) -> dict:
        """Project one immutable :class:`EmissionEntry` onto an ``erpsync_entry``
        column map. Enum fields go in as their stable string values (matching the
        table's CHECK constraints); ``notes`` becomes a JSONB list."""
        return {
            "firm_id": self._firm_id,
            "client_id": self._client_id,
            "doc_entry": entry.doc_entry,
            "line_num": entry.line_num,
            "doc_number": entry.doc_number,
            "category": entry.category,
            "scope": entry.scope.value,
            "basis": entry.basis.value,
            "data_quality": entry.data_quality.value,
            "quantity": entry.quantity,
            "uom": entry.uom,
            "amount": entry.amount,
            "factor_ref": entry.factor_ref,
            "factor_value": entry.factor_value,
            "factor_version": entry.factor_version,
            "rule_id": entry.rule_id,
            "rule_version": entry.rule_version,
            "tco2e": entry.tco2e,
            "source_hash": entry.source_hash,
            "notes": list(entry.notes),
            "status": status,
        }
