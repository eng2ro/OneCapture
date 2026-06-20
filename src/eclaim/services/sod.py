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

    amount = claim.total_amount
    if (
        amount is not None
        and approver.authority_limit is not None
        and amount > approver.authority_limit
    ):
        raise SoDViolation(
            f"amount {amount} exceeds approver authority limit {approver.authority_limit}"
        )
