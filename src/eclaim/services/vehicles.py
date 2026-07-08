"""Vehicle registry (Appendix H-C): client-scoped vehicles for mileage trips.

A separate module rather than a user attribute: a claimant can claim on behalf
of someone else (or paid in advance), so a mileage line picks ANY registered
vehicle — defaulting to the claimant's usual one. ``vehicle_type`` feeds
CarbonNext's distance-based method (Scope 3 Cat 4/6).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import VEHICLE_TYPES, Vehicle

__all__ = ["VEHICLE_TYPES", "resolve", "list_for_clients", "usual_for"]


def resolve(session: Session, client_id: uuid.UUID, vehicle_id) -> Vehicle | None:
    """The registry vehicle for a capture selection — client-scoped and active.
    A missing/foreign/inactive id resolves to None (the trip still files; the
    vehicle type is simply absent, like before the registry existed)."""
    if not vehicle_id:
        return None
    try:
        vid = uuid.UUID(str(vehicle_id))
    except (ValueError, TypeError):
        return None
    v = session.get(Vehicle, vid)
    if v is None or v.client_id != client_id or not v.active:
        return None
    return v


def list_for_clients(session: Session, client_ids, *, active_only: bool = False) -> list[Vehicle]:
    if not client_ids:
        return []
    q = select(Vehicle).where(Vehicle.client_id.in_(client_ids))
    if active_only:
        q = q.where(Vehicle.active.is_(True))
    return list(session.execute(q.order_by(Vehicle.label)).scalars())


def usual_for(session: Session, claimant_id: uuid.UUID | None) -> Vehicle | None:
    """The claimant's usual vehicle (the capture default), if registered."""
    if claimant_id is None:
        return None
    return session.execute(
        select(Vehicle).where(
            Vehicle.usual_claimant_id == claimant_id, Vehicle.active.is_(True)
        ).order_by(Vehicle.created_at).limit(1)
    ).scalar_one_or_none()
