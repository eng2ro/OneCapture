"""CarbonNext ingestion client (IR-6, service identity "company_dataentry").

OneCapture posts released emission batches into CarbonNext as a single service
identity — one ``CARBONNEXT_SERVICE_TOKEN`` for all companies, not a per-company
credential. The destination company is resolved per batch from
``client.carbonnext_company_id``; a client with no mapping cannot be posted.

This spine ships a **stub**: it records what *would* be posted and returns a
synthetic ack — no live HTTP. The real call (retry/backoff, ack handling,
reconciliation) lands with the release/ingestion module. The stub still enforces
the mapping precondition, so unmapped clients fail loudly here and now.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


class CarbonNextError(RuntimeError):
    """Base class for CarbonNext posting errors."""


class UnmappedClientError(CarbonNextError):
    """The client has no ``carbonnext_company_id`` — destination unknown."""


@dataclass(frozen=True)
class CarbonNextAck:
    """What the (stubbed) ingestion endpoint returns for an accepted batch."""

    carbonnext_company_id: str
    batch_id: uuid.UUID
    idempotency_key: str
    accepted_count: int
    receipt: str


@dataclass
class CarbonNextClient:
    """Record-and-ack stub. ``calls`` retains every post for assertion/inspection."""

    api_url: str = "https://carbonnext.example/api"
    service_token: str = ""
    calls: list[CarbonNextAck] = field(default_factory=list)

    def post_emission_entries(
        self,
        carbonnext_company_id: str | None,
        batch_id: uuid.UUID,
        idempotency_key: str,
        entries: list[dict[str, Any]],
    ) -> CarbonNextAck:
        """Post a released batch to a CarbonNext company.

        Raises :class:`UnmappedClientError` when ``carbonnext_company_id`` is
        missing — the destination cannot be guessed, so we never post.
        """
        if not carbonnext_company_id:
            raise UnmappedClientError(
                "client has no carbonnext_company_id; cannot post to CarbonNext"
            )
        ack = CarbonNextAck(
            carbonnext_company_id=carbonnext_company_id,
            batch_id=batch_id,
            idempotency_key=idempotency_key,
            accepted_count=len(entries),
            receipt=f"STUB-CARBONNEXT:{carbonnext_company_id}:{idempotency_key[:16]}",
        )
        self.calls.append(ack)
        return ack
