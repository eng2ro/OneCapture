"""Document intake service (C1): classify → route → record, and manual re-route.

Every captured page is classified by the vision OCR (``document_type`` +
``type_confidence``) and routed by :mod:`eclaim.services.routing`. This service turns
that into a durable :class:`~eclaim.db.models.DocumentIntake` row and an audit event,
so a routing decision is never invisible and a vendor bill is parked in a visible
holding queue rather than forced into e-Claim. Reviewers can correct a route
(:func:`reroute`); the correction is itself an audited routing decision.

Pure routing lives in :mod:`eclaim.services.routing`; this module only persists and
audits. Building an actual e-Claim claim from a corrected page needs an OCR provider +
image bytes, which are request-scoped, so that lives in the web route — this service
just records where the page went and links it to the claim that consumed it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import DocumentIntake
from ..ocr.base import Extraction
from ..repositories import AuditRepository
from . import routing
from .audit import record_event

ENTITY_TYPE = "document_intake"


class IntakeError(RuntimeError):
    """Base for intake-service errors (mapped to 4xx by the routes)."""


class IntakeNotFound(IntakeError):
    pass


class IllegalReroute(IntakeError):
    """A re-route that isn't allowed (bad target, or the row is already consumed)."""


@dataclass(frozen=True)
class Provenance:
    """Where a captured page's image lives + what it was called."""

    sha256: str | None = None
    path: str | None = None
    media_type: str | None = None
    name: str | None = None


def record_intake(
    session: Session,
    *,
    firm_id: uuid.UUID,
    client_id: uuid.UUID,
    created_by_user_id: uuid.UUID | None,
    extraction: Extraction,
    provenance: Provenance,
    actor: str,
    claim_id: uuid.UUID | None = None,
    ingestion_job_id: uuid.UUID | None = None,
    threshold=None,
) -> tuple[DocumentIntake, routing.Route]:
    """Classify + route one captured page, persist the intake record, audit it, and
    (for AP-side pages) try to link a delivery order to a matching vendor invoice.

    ``claim_id`` is the e-Claim claim the page became a line of, when it routed to
    e-Claim — the row is then recorded ``consumed``. AP-holding / pending pages stay
    ``open`` for the holding queue (or a manual route)."""
    decision = routing.route(
        extraction.document_type, extraction.type_confidence, threshold=threshold
    )
    to_eclaim = decision.queue == routing.QUEUE_ECLAIM

    # De-dup the holding queue: re-capturing the SAME image (identical bytes → same
    # sha256) must not pile up identical rows — a common case when someone uploads the
    # same bill twice. If an OPEN holding intake for this image already exists for the
    # client, return it (idempotent) instead of adding a duplicate. Only for diverted
    # pages; an e-Claim page becomes a claim line, not a holding row.
    if not to_eclaim and provenance.sha256:
        existing = session.execute(
            select(DocumentIntake).where(
                DocumentIntake.client_id == client_id,
                DocumentIntake.image_sha256 == provenance.sha256,
                DocumentIntake.status == "open",
            ).order_by(DocumentIntake.created_at).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, decision

    intake = DocumentIntake(
        firm_id=firm_id,
        client_id=client_id,
        created_by_user_id=created_by_user_id,
        image_sha256=provenance.sha256,
        image_path=provenance.path,
        media_type=provenance.media_type,
        source_name=provenance.name,
        document_type=extraction.document_type,
        type_confidence=extraction.type_confidence,
        type_signals=list(extraction.type_signals or []),
        routed_to=decision.queue,
        routed_by="system",
        needs_manual=decision.needs_manual,
        status="consumed" if (to_eclaim and claim_id is not None) else "open",
        link_key=routing.link_key(extraction.vendor, extraction.po_ref),
        claim_id=claim_id if to_eclaim else None,
        ingestion_job_id=ingestion_job_id,
        vendor=extraction.vendor,
        doc_no=extraction.doc_no,
        total_amount=extraction.total_amount,
        currency=extraction.currency,
    )
    session.add(intake)
    session.flush()
    _link_counterpart(session, intake)
    _audit(
        session, intake, "intake_routed", actor,
        {
            "document_type": intake.document_type,
            "type_confidence": None if intake.type_confidence is None else str(intake.type_confidence),
            "routed_to": intake.routed_to,
            "routed_by": intake.routed_by,
            "needs_manual": intake.needs_manual,
            "signals": intake.type_signals,
        },
    )
    return intake, decision


def holding_queue(session: Session, client_ids) -> list[DocumentIntake]:
    """Open AP-holding + still-pending pages awaiting the AP module or a manual route,
    restricted to the principal's visible clients (app-layer narrowing on top of RLS).
    e-Claim-routed pages are NOT here — they became claims."""
    if not client_ids:
        return []
    return list(
        session.execute(
            select(DocumentIntake)
            .where(
                DocumentIntake.client_id.in_(client_ids),
                DocumentIntake.status == "open",
                DocumentIntake.routed_to.in_(
                    (routing.QUEUE_AP_HOLDING, routing.QUEUE_PENDING)
                ),
            )
            .order_by(DocumentIntake.created_at, DocumentIntake.id)
        ).scalars()
    )


def holding_count(session: Session, client_ids) -> int:
    """COUNT of open holding-queue rows — for the nav badge, so the sidebar doesn't
    load every intake row on every page render (F9)."""
    if not client_ids:
        return 0
    return int(session.execute(
        select(func.count()).select_from(DocumentIntake).where(
            DocumentIntake.client_id.in_(client_ids),
            DocumentIntake.status == "open",
            DocumentIntake.routed_to.in_((routing.QUEUE_AP_HOLDING, routing.QUEUE_PENDING)),
        )
    ).scalar_one())


def get_intake(session: Session, intake_id: uuid.UUID) -> DocumentIntake:
    row = session.get(DocumentIntake, intake_id)
    if row is None:
        raise IntakeNotFound(str(intake_id))
    return row


def reroute(
    session: Session,
    *,
    intake_id: uuid.UUID,
    to: str,
    actor: str,
    claim_id: uuid.UUID | None = None,
) -> DocumentIntake:
    """A reviewer's correction: re-route an open page to a different queue. Records the
    new route (``routed_by='user'``) and an audit event carrying the before→after — the
    correction is itself a routing decision (C1). When ``to`` is e-Claim the caller has
    already built the claim and passes its ``claim_id``; the row is marked consumed."""
    row = get_intake(session, intake_id)
    if row.status == "consumed":
        raise IllegalReroute("this page has already been processed and cannot be re-routed")
    if to not in (routing.QUEUE_ECLAIM, routing.QUEUE_AP_HOLDING, routing.QUEUE_PENDING):
        raise IllegalReroute(f"unknown route target {to!r}")

    before = row.routed_to
    row.routed_to = to
    row.routed_by = "user"
    row.needs_manual = to == routing.QUEUE_PENDING
    if to == routing.QUEUE_ECLAIM and claim_id is not None:
        row.claim_id = claim_id
        row.status = "consumed"
    _audit(
        session, row, "intake_rerouted", actor,
        {"from": before, "to": to, "routed_by": "user"},
    )
    session.flush()
    return row


def _link_counterpart(session: Session, intake: DocumentIntake) -> None:
    """Link a delivery order to its matching vendor invoice (same vendor + PO/DO ref),
    both directions, when the counterpart is already captured. The DO alone is not
    payable — the link is what lets the AP module pay the invoice against its DO."""
    if not intake.link_key or intake.document_type not in ("vendor_invoice", "delivery_order"):
        return
    want = "delivery_order" if intake.document_type == "vendor_invoice" else "vendor_invoice"
    counterpart = session.execute(
        select(DocumentIntake)
        .where(
            DocumentIntake.client_id == intake.client_id,
            DocumentIntake.link_key == intake.link_key,
            DocumentIntake.document_type == want,
            DocumentIntake.id != intake.id,
            DocumentIntake.linked_intake_id.is_(None),
        )
        .order_by(DocumentIntake.created_at)
        .limit(1)
    ).scalar_one_or_none()
    if counterpart is not None:
        intake.linked_intake_id = counterpart.id
        counterpart.linked_intake_id = intake.id


def _audit(session: Session, intake: DocumentIntake, event_type: str, actor: str, detail: dict) -> None:
    record_event(
        AuditRepository(session),
        firm_id=intake.firm_id,
        client_id=intake.client_id,
        entity_type=ENTITY_TYPE,
        entity_id=intake.id,
        event_type=event_type,
        actor=actor,
        detail=detail,
    )
