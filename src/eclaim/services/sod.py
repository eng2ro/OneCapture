"""Separation-of-duties guard for claim approval (spec §5).

The dynamic checks live here (they depend on the live principal): an approver
may not approve a claim they created, must hold access to the claim's client,
must not be a Viewer, and may not approve an amount above their authority limit.
The static ``approved_by <> created_by`` rule is *also* a DB CHECK — this is the
defence-in-depth second layer at the application boundary.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from ..auth.principal import Principal
from ..db.models import ApprovalMatrixRule, Claim
from ..repositories import AuditRepository
from ..tenancy import set_tenant_context
from .audit import record_event
from .claims import ClaimError


class SoDViolation(ClaimError):
    """An approval that violates separation of duties / authority (API → 403)."""


# Role adequacy for the approval matrix (Appendix B): a higher role satisfies a
# rule that requires a lower one (a partner can sign off what a manager can).
_ROLE_RANK = {"viewer": 0, "approver": 1, "manager": 2, "partner": 3}


def _rule_satisfied(rule: ApprovalMatrixRule, approver: Principal) -> bool:
    """Does ``approver`` meet this step's requirement — the named person, or a role
    at least as senior as the one required? A rule with neither is any approver."""
    if rule.approver_user_id is not None:
        return approver.user_id == rule.approver_user_id
    if rule.approver_role:
        return _ROLE_RANK.get(approver.base_role, -1) >= _ROLE_RANK.get(rule.approver_role, 99)
    return True


def _in_band(amount: Decimal, rule: ApprovalMatrixRule) -> bool:
    return (rule.min_amount is None or amount >= rule.min_amount) and (
        rule.max_amount is None or amount <= rule.max_amount
    )


def select_matrix_rule(rules, *, amount, department, category_ids) -> ApprovalMatrixRule | None:
    """The most specific active ``step_order = 1`` rule whose amount band and scope
    fit this claim, or None. Phase-1 rules carry NULL scopes (apply to all); the
    scope checks are here so Phase-2 per-department / per-category rows just work.
    A more specific rule (category, then department) wins over a general one."""
    matches = [
        r for r in rules
        if r.active and r.step_order == 1 and _in_band(amount, r)
        and (r.scope_department is None or r.scope_department == department)
        and (r.scope_category_id is None or r.scope_category_id in category_ids)
    ]
    matches.sort(
        key=lambda r: (r.scope_category_id is not None, r.scope_department is not None),
        reverse=True,
    )
    return matches[0] if matches else None


def matrix_rule_for(repos, claim: Claim) -> ApprovalMatrixRule | None:
    """Resolve the approval-matrix rule that governs this claim, or None when the
    client has configured no matrix (→ legacy behaviour: any authorised approver
    within their personal authority_limit)."""
    rules = repos.approvals.rules_for_client(claim.client_id)
    if not rules:
        return None
    amount = claim.total_claimed if claim.total_claimed is not None else claim.total_amount
    cats = {ln.category_id for ln in repos.claims.lines(claim.id) if ln.category_id}
    return select_matrix_rule(
        rules, amount=amount if amount is not None else Decimal(0),
        department=claim.department, category_ids=cats,
    )


def _describe_rule(rule: ApprovalMatrixRule) -> str:
    # Phase-1 enforces exactly ONE approval per band. ``approvals_required`` is not
    # yet honoured (multi-approval chains are Phase-2), so we never render an "N×"
    # count here — it would promise a control the engine does not enforce, the exact
    # mismatch punch-list R1 closes (legacy >1 rows are also clamped to 1 by
    # migration 0024). Restore the count wording when Phase-2 lands the real chain.
    if rule.approver_user_id is not None:
        return "a specific approver"
    if rule.approver_role:
        return f"a {rule.approver_role}"
    return "an authorised approver"


def check_can_approve(
    claim: Claim, approver: Principal, *, matrix_rule: ApprovalMatrixRule | None = None
) -> None:
    """Raise :class:`SoDViolation` if ``approver`` may not approve ``claim``.

    ``matrix_rule`` is the pre-resolved Appendix-B rule for this claim (via
    :func:`matrix_rule_for`); when given, the approver must satisfy it (required
    role/person) on top of the base separation-of-duties + personal authority_limit
    checks. Kept pure (no DB) so the UI predicate and the real gate share it."""
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

    # Appendix B: the configured approval matrix takes priority for who may sign off
    # this band. authority_limit above remains an optional extra personal cap.
    if matrix_rule is not None and not _rule_satisfied(matrix_rule, approver):
        raise SoDViolation(
            f"approval of {amount} requires {_describe_rule(matrix_rule)} "
            "under this client's approval matrix"
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
        check_can_approve(claim, approver, matrix_rule=matrix_rule_for(repos, claim))
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


def can_approve(
    claim: Claim, approver: Principal, *, matrix_rule: ApprovalMatrixRule | None = None
) -> bool:
    """Non-raising predicate over :func:`check_can_approve` — for the UI to decide
    whether to draw the review actions. Pass the resolved ``matrix_rule`` so the
    button reflects the approval matrix; the service stays the real gate."""
    try:
        check_can_approve(claim, approver, matrix_rule=matrix_rule)
        return True
    except SoDViolation:
        return False
