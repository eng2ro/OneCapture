"""Separation-of-duties guard for claim approval (spec §5).

The dynamic checks live here (they depend on the live principal): an approver
may not approve a claim they created, must hold access to the claim's client,
must not be a Viewer, and may not approve an amount above their authority limit.
The static ``approved_by <> created_by`` rule is *also* a DB CHECK — this is the
defence-in-depth second layer at the application boundary.
"""

from __future__ import annotations

from ..auth.principal import Principal
from ..db.models import Claim
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
    """Run :func:`check_can_approve` and, if it fails, persist a COMMITTED
    ``approval_denied`` audit event before re-raising — so a blocked attempt
    (maker==checker, over-authority, viewer, no client grant) is never invisible to
    auditors. The guard runs before any business mutation, so the only pending change
    committed here is this single audit event; the caller still gets the 403/error.

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
            record_event(
                repos.audit,
                firm_id=claim.firm_id,
                client_id=claim.client_id,
                entity_type="claim",
                entity_id=claim.id,
                event_type="approval_denied",
                actor=approver.email or str(approver.user_id),
                detail={"action": action, "reason": str(exc)},
            )
            repos.session.commit()
        raise


def can_approve(claim: Claim, approver: Principal) -> bool:
    """Non-raising predicate over :func:`check_can_approve` — for the UI to decide
    whether to draw the review actions. The service stays the real gate."""
    try:
        check_can_approve(claim, approver)
        return True
    except SoDViolation:
        return False
