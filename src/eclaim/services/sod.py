"""Separation-of-duties guard for claim approval (spec §5).

The dynamic checks live here (they depend on the live principal): an approver
may not approve a claim they created, must hold access to the claim's client,
must not be a Viewer, and may not approve an amount above their authority limit.
The static ``approved_by <> created_by`` rule is *also* a DB CHECK — this is the
defence-in-depth second layer at the application boundary.
"""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from ..auth.principal import Principal
from ..db.models import Claim
from ..repositories import AuditRepository
from ..tenancy import set_tenant_context
from .audit import record_event
from .claims import ClaimError


class SoDViolation(ClaimError):
    """An approval that violates separation of duties / authority (API → 403)."""


def check_can_approve(claim: Claim, approver: Principal) -> None:
    """Raise :class:`SoDViolation` if ``approver`` may not approve ``claim``."""
    if approver.base_role == "viewer":
        raise SoDViolation("viewers cannot approve claims")

    if not approver.can_access_client(claim.client_id):
        raise SoDViolation("approver has no grant to this client")

    if (
        claim.created_by_user_id is not None
        and claim.created_by_user_id == approver.user_id
    ):
        raise SoDViolation("the user who created a claim cannot approve it")

    # The claim's rolled-up header total drives the authority gate (the per-line
    # amounts sum onto total_claimed). Falls back to the legacy total_amount for
    # pre-redesign rows that have no header total yet.
    amount = claim.total_claimed if claim.total_claimed is not None else claim.total_amount
    if (
        amount is not None
        and approver.authority_limit is not None
        and amount > approver.authority_limit
    ):
        raise SoDViolation(
            f"amount {amount} exceeds approver authority limit {approver.authority_limit}"
        )


def authorize_approval(repos, claim: Claim, approver: Principal, *, action: str) -> None:
    """Run :func:`check_can_approve` and, if it fails, persist a durable
    ``approval_denied`` audit event before re-raising — so a blocked attempt
    (maker==checker, over-authority, viewer, no client grant) is never invisible to
    auditors. The caller still gets the 403/error.

    The denial is written in a SEPARATE short-lived transaction (see
    :func:`_record_denied`), never by committing ``repos.session``: both the API and
    the web layer roll the request transaction back on ``SoDViolation``, so the event
    must live outside it to survive — and committing the request session here would
    flush any pending business work with it, breaking the "one request = one atomic
    transaction" contract (blocker B5).

    Use this (not bare ``check_can_approve``) on the real approval transitions. The
    non-raising :func:`can_approve` predicate stays audit-free — it's only the UI
    deciding whether to draw a button, not an attempted sign-off."""
    try:
        check_can_approve(claim, approver)
    except SoDViolation as exc:
        # The denial event lives on the claim's tenant audit chain, so it can only
        # be written when the actor actually has access to that client (the
        # maker==checker, over-authority and viewer-with-grant cases). A cross-tenant
        # attempt with no grant cannot — and must not — write into another client's
        # chain under RLS; the attempt fails closed either way.
        if approver.can_access_client(claim.client_id):
            _record_denied(repos, claim, approver, action=action, reason=str(exc))
        raise


def _record_denied(
    repos, claim: Claim, approver: Principal, *, action: str, reason: str
) -> None:
    """Persist the ``approval_denied`` event in its own short-lived transaction, so a
    blocked attempt is durable even though the request transaction is about to roll
    back — WITHOUT committing ``repos.session`` (which would flush partial business
    work; blocker B5).

    The new session shares the request session's *bind*: in production that bind is the
    engine, so this opens its own connection and commits independently of the request;
    under the test harness the bind is the pinned per-test connection, so the write
    lands in a nested savepoint that the suite's outer rollback still reverts — nothing
    leaks. A fresh session starts with no tenant context, so RLS is re-primed from the
    approver (who, in this branch, provably has access to ``claim.client_id`` and thus
    its firm) before the append-only insert."""
    factory = sessionmaker(
        bind=repos.session.get_bind(), expire_on_commit=False, future=True
    )
    audit_session = factory()
    try:
        set_tenant_context(audit_session, approver.firm_id, approver.allowed_client_ids)
        record_event(
            AuditRepository(audit_session),
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="approval_denied",
            actor=approver.email or str(approver.user_id),
            detail={"action": action, "reason": reason},
        )
        audit_session.commit()
    finally:
        audit_session.close()


def can_approve(claim: Claim, approver: Principal) -> bool:
    """Non-raising predicate over :func:`check_can_approve` — for the UI to decide
    whether to draw the review actions. The service stays the real gate."""
    try:
        check_can_approve(claim, approver)
        return True
    except SoDViolation:
        return False
