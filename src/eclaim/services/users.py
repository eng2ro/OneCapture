"""Firm user administration — the login-user registry (Appendix I-B).

Users are registered and managed IN OneCapture (owner decision 2026-07-07);
CarbonNext activation will later provision each company's first identity. This
module owns only the directory row (name, role, authority limit, status) and
the client grants — credentials stay with the AuthProvider, so nothing here
ever reads or writes a password.

Lockout guards (never configurable): you cannot deactivate yourself and you
cannot demote yourself out of firm scope. Since only an active firm-scoped
user can reach this service, those two rules alone guarantee every firm always
keeps at least one active partner/manager.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.principal import Principal
from ..db.models import (
    BASE_ROLES,
    FIRM_SCOPED_ROLES,
    AppUser,
    UserClientGrant,
)
from ..repositories import AuditRepository
from .audit import record_event
from .sod import _ROLE_RANK


class UserAdminError(Exception):
    """A friendly, user-facing refusal (rendered on the admin page, never a 500)."""


def grants_by_user(session: Session, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """client ids granted per user — feeds the list view and the edit form."""
    if not user_ids:
        return {}
    rows = session.execute(
        select(UserClientGrant.user_id, UserClientGrant.client_id).where(
            UserClientGrant.user_id.in_(user_ids)
        )
    ).all()
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for user_id, client_id in rows:
        out.setdefault(user_id, []).append(client_id)
    return out


def _parse_limit(raw: str) -> Decimal | None:
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise UserAdminError("The authority limit must be a number (or empty for no cap).")
    if not value.is_finite():                # 'NaN'/'Infinity' construct fine but
        raise UserAdminError(                # comparing/storing them would 500
            "The authority limit must be a number (or empty for no cap)."
        )
    if value < 0:
        raise UserAdminError("The authority limit cannot be negative.")
    if value >= Decimal("1000000000000"):    # Numeric(14,2): at most 12 integer digits
        raise UserAdminError("The authority limit is too large.")
    return value


def save_user(
    session: Session,
    *,
    principal: Principal,
    audit_client_id: uuid.UUID,
    user_id: uuid.UUID | None,
    email: str,
    display_name: str,
    base_role: str,
    authority_limit: str,
    status: str,
    grant_client_ids: list[uuid.UUID],
    actor: str,
) -> AppUser:
    """Create (``user_id is None``) or update one firm user, audited old → new.

    ``audit_client_id`` anchors the audit event (audit_event.client_id is NOT
    NULL); a user is a firm-level entity, so the caller passes the firm's
    default client.
    """
    email = (email or "").strip().lower()
    display_name = (display_name or "").strip()
    if not email or "@" not in email:
        raise UserAdminError("A valid email is required — it is how the user signs in.")
    if not display_name:
        raise UserAdminError("A display name is required.")
    if base_role not in BASE_ROLES:
        raise UserAdminError(f"Unknown role {base_role!r}.")
    if status not in ("active", "inactive"):
        raise UserAdminError("Status must be active or inactive.")
    limit = _parse_limit(authority_limit)

    # No minting upward: nobody assigns a role senior to their own, so a manager
    # can never create (or promote anyone to) partner.
    if _ROLE_RANK.get(base_role, 99) > _ROLE_RANK.get(principal.base_role, -1):
        raise UserAdminError(
            "You cannot assign a role senior to your own — ask a partner."
        )

    firm_scoped = base_role in FIRM_SCOPED_ROLES
    if firm_scoped:
        grant_client_ids = []          # partner/manager see every client already
    else:
        if not grant_client_ids:
            raise UserAdminError(
                "Pick at least one company — an approver/viewer only sees the "
                "companies granted here."
            )
        for cid in grant_client_ids:
            if not principal.can_access_client(cid):
                raise UserAdminError("You cannot grant a company outside your firm.")

    # Duplicate email pre-check (friendly message before the DB constraint fires).
    dup = session.execute(
        select(AppUser).where(
            AppUser.firm_id == principal.firm_id, AppUser.email == email
        )
    ).scalar_one_or_none()
    if dup is not None and (user_id is None or dup.id != user_id):
        raise UserAdminError("A user with that email already exists in this firm.")

    if user_id is None:
        user = AppUser(
            firm_id=principal.firm_id, email=email, display_name=display_name,
            base_role=base_role, authority_limit=limit, status=status,
        )
        session.add(user)
        session.flush()
        event_type, detail = "user_created", {
            "email": email, "role": base_role, "status": status,
            "authority_limit": str(limit) if limit is not None else None,
        }
    else:
        user = session.get(AppUser, user_id)
        if user is None or user.firm_id != principal.firm_id:
            raise UserAdminError("User not found.")
        if _ROLE_RANK.get(user.base_role, 99) > _ROLE_RANK.get(principal.base_role, -1):
            raise UserAdminError(
                "You cannot edit a user senior to your own role."
            )
        if user.id == principal.user_id:
            if status != "active":
                raise UserAdminError("You cannot deactivate your own account.")
            if base_role not in FIRM_SCOPED_ROLES:
                raise UserAdminError(
                    "You cannot demote your own account out of the admin roles — "
                    "ask another partner or manager to do it."
                )
            # Symmetric lockout: no self-service escalation either. Your role and
            # your approval cap are only ever changed by SOMEONE ELSE.
            if base_role != user.base_role:
                raise UserAdminError(
                    "You cannot change your own role — ask another partner or "
                    "manager to do it."
                )
            if limit != user.authority_limit:
                raise UserAdminError(
                    "You cannot change your own authority limit — ask another "
                    "partner or manager to do it."
                )
        changes = {}
        for field, new in (
            ("email", email), ("display_name", display_name),
            ("base_role", base_role), ("authority_limit", limit), ("status", status),
        ):
            old = getattr(user, field)
            if old != new:
                changes[field] = {"from": str(old) if old is not None else None,
                                  "to": str(new) if new is not None else None}
                setattr(user, field, new)
        session.flush()
        event_type, detail = "user_updated", changes

    # Replace the grant set (firm-scoped roles keep none — no stale hidden access
    # to inherit if the user is later demoted to a client-scoped role).
    existing = grants_by_user(session, [user.id]).get(user.id, [])
    wanted = set(grant_client_ids)
    if set(existing) != wanted:
        for row in session.execute(
            select(UserClientGrant).where(UserClientGrant.user_id == user.id)
        ).scalars():
            if row.client_id not in wanted:
                session.delete(row)
        for cid in wanted - set(existing):
            session.add(UserClientGrant(
                firm_id=principal.firm_id, user_id=user.id, client_id=cid,
            ))
        session.flush()
        detail["grants"] = {"from": sorted(str(c) for c in existing),
                            "to": sorted(str(c) for c in wanted)}

    if detail:                          # a no-op edit writes no audit noise
        record_event(
            AuditRepository(session), firm_id=principal.firm_id,
            client_id=audit_client_id, entity_type="app_user", entity_id=user.id,
            event_type=event_type, actor=actor or "system", detail=detail,
        )
        session.flush()
    return user
