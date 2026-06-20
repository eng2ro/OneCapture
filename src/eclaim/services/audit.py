"""Audit-event recording over the shared hash chain (:mod:`core.audit`).

Each call reads the chain tip for the entity, computes
``hash = chain_hash(prev_hash, payload)``, and persists the event. For an
operation that writes several events at once, pass the previous event's hash
back in as ``prev_hash`` so the chain stays linked without re-reading the tip
(robust to same-transaction ``now()`` ties).
"""

from __future__ import annotations

import uuid
from typing import Any

from core.audit import chain_hash

from ..db.models import AuditEvent
from ..repositories import AuditRepository


def record_event(
    audit: AuditRepository,
    *,
    firm_id: uuid.UUID,
    client_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    event_type: str,
    actor: str,
    detail: dict[str, Any] | None = None,
    prev_hash: str | None = None,
    ip: str | None = None,
    device: str | None = None,
) -> AuditEvent:
    if prev_hash is None:
        prev_hash = audit.tip_hash(entity_type, entity_id)
    payload = {
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "event_type": event_type,
        "actor": actor,
        "detail": detail or {},
    }
    digest = chain_hash(prev_hash, payload)
    return audit.add(
        AuditEvent(
            firm_id=firm_id,
            client_id=client_id,
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            actor=actor,
            detail=detail,
            prev_hash=prev_hash,
            hash=digest,
            ip=ip,
            device=device,
        )
    )
