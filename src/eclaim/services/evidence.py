"""Evidence-pack assembly (the data half, no PDF).

:func:`EvidenceService.build` gathers everything a per-claim evidence pack needs
from stored data — claim, category, claimant, the full hash-chained audit trail,
and the original release batch (hash + TSA token, if released) — RLS-scoped via
the request ``repos``, and returns a structured, immutable :class:`Evidence`.

It is deliberately *deterministic*: it reads only persisted rows and carries no
"generated-at" timestamp (that is a render-time argument), so regenerating the
pack yields byte-identical assembled content. The PDF renderer
(:mod:`eclaim.services.evidence_pdf`) consumes this model — keeping the data
assembly testable without any PDF dependency.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from ..db.models import Claimant, Client, EmissionEntry, ReleaseBatch
from .claims import ClaimNotFound, Repos


@dataclass(frozen=True)
class EvidenceEvent:
    """One audit event, flattened for the approval trail + integrity section."""

    event_type: str
    actor: str
    created_at: datetime
    reason: str | None
    hash: str
    prev_hash: str | None


@dataclass(frozen=True)
class Evidence:
    """The assembled evidence for one claim — everything but the generated-at
    timestamp (a render arg), so two builds compare equal."""

    # Header
    claim_id: uuid.UUID
    client_id: uuid.UUID
    client_name: str
    status: str

    # Confirmed fields
    vendor: str | None
    doc_no: str | None
    doc_date: str | None
    currency: str | None
    total_amount: Decimal | None
    quantity: Decimal | None
    unit: str | None
    category_name: str | None
    scope: int | None
    factor_key: str | None
    factor_version: int | None
    tco2e: Decimal | None
    data_quality: str | None

    # Claimant
    claimant_name: str | None
    employee_ref: str | None
    cost_centre: str | None

    # Source document
    image_path: str
    image_sha256: str

    # Approval trail (genesis → tip)
    trail: tuple[EvidenceEvent, ...]

    # Integrity
    batch_hash: str | None
    tsa_token: str | None

    @property
    def released(self) -> bool:
        return self.batch_hash is not None


class EvidenceService:
    """Assembles a claim's evidence model from stored data (no PDF)."""

    @staticmethod
    def build(repos: Repos, claim_id: uuid.UUID) -> Evidence:
        claim = repos.claims.get(claim_id)
        if claim is None:
            raise ClaimNotFound(str(claim_id))
        session = repos.session

        client = session.get(Client, claim.client_id)
        category = (
            repos.categories.get_by_id(claim.category_id) if claim.category_id else None
        )
        claimant = (
            session.get(Claimant, claim.submitted_by_claimant_id)
            if claim.submitted_by_claimant_id
            else None
        )

        trail = tuple(
            EvidenceEvent(
                event_type=e.event_type,
                actor=e.actor,
                created_at=e.created_at,
                reason=(e.detail or {}).get("reason"),
                hash=e.hash,
                prev_hash=e.prev_hash,
            )
            for e in repos.audit.chain("claim", claim_id)
        )

        # The claim's original release batch — earliest ledger entry, so a later
        # reversal entry doesn't override the original release hash/TSA.
        entry = session.execute(
            select(EmissionEntry)
            .where(
                EmissionEntry.source_type == "eclaim",
                EmissionEntry.source_id == claim.id,
            )
            .order_by(EmissionEntry.created_at)
            .limit(1)
        ).scalar_one_or_none()
        batch = session.get(ReleaseBatch, entry.release_batch_id) if entry else None

        return Evidence(
            claim_id=claim.id,
            client_id=claim.client_id,
            client_name=client.name if client else "",
            status=claim.status,
            vendor=claim.vendor,
            doc_no=claim.doc_no,
            doc_date=claim.doc_date,
            currency=claim.currency,
            total_amount=claim.total_amount,
            quantity=claim.quantity,
            unit=claim.unit,
            category_name=category.name if category else None,
            scope=claim.scope,
            factor_key=claim.factor_key,
            factor_version=claim.factor_version,
            tco2e=claim.tco2e,
            data_quality=claim.data_quality,
            claimant_name=claimant.name if claimant else None,
            employee_ref=claimant.employee_ref if claimant else None,
            cost_centre=claimant.cost_centre if claimant else None,
            image_path=claim.image_path,
            image_sha256=claim.image_sha256,
            trail=trail,
            batch_hash=batch.batch_hash if batch else None,
            tsa_token=batch.tsa_token if batch else None,
        )
