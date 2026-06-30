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

from ..db.models import CarbonHandoff, Claimant, Client, ReleaseBatch
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
    # e-Claim does no carbon maths — just whether this line forwards to CarbonNext.
    carbon_relevant: bool | None

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

        # Carbon + receipt fields live on the lines now; the evidence pack shows the
        # claim's first line (single-receipt claims are the common case).
        line = repos.claims.first_line(claim_id)
        client = session.get(Client, claim.client_id)
        category = (
            repos.categories.get_by_id(line.category_id)
            if line and line.category_id
            else None
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

        # The claim's original release batch — earliest FORWARD handoff for any of
        # its lines, so a later reversal doesn't override the original hash/TSA.
        line_ids = [ln.id for ln in repos.claims.lines(claim_id)]
        handoff = (
            session.execute(
                select(CarbonHandoff)
                .where(
                    CarbonHandoff.line_id.in_(line_ids),
                    CarbonHandoff.direction == "forward",
                )
                .order_by(CarbonHandoff.created_at)
                .limit(1)
            ).scalar_one_or_none()
            if line_ids
            else None
        )
        batch = session.get(ReleaseBatch, handoff.release_batch_id) if handoff else None

        return Evidence(
            claim_id=claim.id,
            client_id=claim.client_id,
            client_name=client.name if client else "",
            status=claim.status,
            vendor=line.vendor if line else None,
            doc_no=line.doc_no if line else None,
            doc_date=line.doc_date if line else None,
            currency=line.currency if line else None,
            total_amount=line.total_amount if line else claim.total_claimed,
            quantity=line.quantity if line else None,
            unit=line.unit if line else None,
            category_name=category.name if category else None,
            carbon_relevant=line.carbon_relevant if line else None,
            claimant_name=claimant.name if claimant else None,
            employee_ref=claimant.employee_ref if claimant else None,
            cost_centre=claimant.cost_centre if claimant else None,
            image_path=line.image_path if line else "",
            image_sha256=line.image_sha256 if line else "",
            trail=trail,
            batch_hash=batch.batch_hash if batch else None,
            tsa_token=batch.tsa_token if batch else None,
        )
