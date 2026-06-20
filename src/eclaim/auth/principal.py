"""The request principal and the claimant resolver.

A :class:`Principal` is a firm user's resolved identity for one request: who
they are, their firm, role, and the concrete set of client ids they may touch
(all firm clients if firm-scoped; the grant set if client-scoped). It is the
single object the API and the tenant-context plumbing read from.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db.models import (
    CLIENT_SCOPED_ROLES,
    FIRM_SCOPED_ROLES,
    AppUser,
    Claimant,
    Client,
    UserClientGrant,
)


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    firm_id: uuid.UUID
    base_role: str
    allowed_client_ids: frozenset[uuid.UUID]
    authority_limit: Decimal | None = None
    email: str | None = None

    @property
    def is_firm_scoped(self) -> bool:
        return self.base_role in FIRM_SCOPED_ROLES

    def can_access_client(self, client_id: uuid.UUID) -> bool:
        return client_id in self.allowed_client_ids


def resolve_allowed_client_ids(
    session: Session, *, firm_id: uuid.UUID, user_id: uuid.UUID, base_role: str
) -> frozenset[uuid.UUID]:
    """Firm-scoped roles see every client in the firm; client-scoped roles see
    only the clients they hold a grant for.

    Must run with ``app.current_firm`` already set (the client/grant tables are
    firm-scoped by RLS), which the principal bootstrap guarantees.
    """
    if base_role in FIRM_SCOPED_ROLES:
        rows = session.execute(select(Client.id).where(Client.firm_id == firm_id)).scalars()
    elif base_role in CLIENT_SCOPED_ROLES:
        rows = session.execute(
            select(UserClientGrant.client_id).where(UserClientGrant.user_id == user_id)
        ).scalars()
    else:  # pragma: no cover - guarded upstream by the role CHECK
        return frozenset()
    return frozenset(rows)


# --------------------------------------------------------------------------- #
# App-layer directory narrowing
# --------------------------------------------------------------------------- #
# RLS on client / app_user / user_client_grant is *firm-gated only* — it has to
# be, because principal bootstrap must read the firm-wide client roster before
# the allowed-client set is known. So for a client-scoped role (Approver/Viewer)
# the database will happily return every client and user in the firm. Narrowing
# those listings down to the caller's granted clients is therefore the app
# layer's job, enforced here so no route can forget it.


def list_visible_clients(session: Session, principal: Principal) -> list[Client]:
    """Clients the principal may see. Firm-scoped roles get the whole firm
    roster; client-scoped roles get only the clients they hold a grant for.

    The firm_id filter is belt-and-suspenders: RLS already firm-gates the table,
    but filtering explicitly keeps the narrowing correct even when the query runs
    as a BYPASSRLS/owner connection (e.g. local poking, tests)."""
    stmt = select(Client).where(Client.firm_id == principal.firm_id)
    if not principal.is_firm_scoped:
        # in_(()) is an empty-set predicate → zero rows, which is the right answer
        # for a client-scoped user with no grants.
        stmt = stmt.where(Client.id.in_(principal.allowed_client_ids))
    return list(session.execute(stmt.order_by(Client.name)).scalars())


def list_visible_users(session: Session, principal: Principal) -> list[AppUser]:
    """Firm users the principal may see. Firm-scoped roles see the whole firm
    directory; client-scoped roles see only colleagues who share at least one of
    their granted clients (plus themselves), never the rest of the firm."""
    stmt = select(AppUser).where(AppUser.firm_id == principal.firm_id)
    if not principal.is_firm_scoped:
        peers = (
            select(UserClientGrant.user_id)
            .where(UserClientGrant.client_id.in_(principal.allowed_client_ids))
            .scalar_subquery()
        )
        stmt = stmt.where(
            or_(AppUser.id == principal.user_id, AppUser.id.in_(peers))
        )
    return list(session.execute(stmt.order_by(AppUser.display_name)).scalars())


# --------------------------------------------------------------------------- #
# Claimant resolution (submitters never authenticate — channel binding only)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ClaimantQuarantine:
    """Returned when a channel value matches no known claimant. The intake
    module surfaces this — an unknown sender is quarantined, never dropped."""

    channel_value: str
    reason: str = "unknown_sender"


def resolve_claimant(
    session: Session, *, firm_id: uuid.UUID, client_id: uuid.UUID, channel_value: str
) -> Claimant | ClaimantQuarantine:
    """Match a WhatsApp phone / email to a claimant within a client, else
    quarantine. This is all the spine owns for claimants (intake is a module)."""
    row = session.execute(
        select(Claimant).where(
            Claimant.firm_id == firm_id,
            Claimant.client_id == client_id,
            Claimant.status == "active",
            or_(Claimant.phone == channel_value, Claimant.email == channel_value),
        )
    ).scalar_one_or_none()
    if row is None:
        return ClaimantQuarantine(channel_value=channel_value)
    return row


def build_principal(session: Session, claims: dict) -> Principal:
    """Construct a :class:`Principal` from verified token claims by loading the
    user and resolving the allowed client set. Assumes firm context is set."""
    user_id = uuid.UUID(str(claims["user_id"]))
    firm_id = uuid.UUID(str(claims["firm_id"]))
    user = session.get(AppUser, user_id)
    if user is None or user.status != "active" or user.firm_id != firm_id:
        from .tokens import TokenError

        raise TokenError("unknown or inactive user")
    allowed = resolve_allowed_client_ids(
        session, firm_id=firm_id, user_id=user_id, base_role=user.base_role
    )
    return Principal(
        user_id=user_id,
        firm_id=firm_id,
        base_role=user.base_role,
        allowed_client_ids=allowed,
        authority_limit=user.authority_limit,
        email=user.email,
    )
