"""Clamp legacy approval_matrix_rule.approvals_required > 1 rows to 1

Revision ID: 0024_clamp_legacy_approvals
Revises: 0023_approval_matrix_rule
Create Date: 2026-07-07

Phase-1 enforces exactly ONE approval per amount band — the engine
(``sod.select_matrix_rule``) reads only ``step_order = 1`` and never counts
``approvals_required``. The launch templates were fixed to seed
``approvals_required = 1`` (PHASE1_APPROVALS_REQUIRED), but any row written before
that fix (or by a crafted POST) can still carry ``> 1`` — a promised-but-unenforced
"2× a partner" control that ``_describe_rule`` used to surface in denial messages.

This one-shot data migration clamps every such legacy row down to 1 so the stored
data matches what the engine actually enforces. Multi-approval chains are Phase-2;
they will reintroduce ``> 1`` deliberately alongside the enforcement, not as stale
residue. Idempotent (re-running touches nothing) and safe to run repeatedly.

Runs on the privileged migration connection (the same owner/superuser that created
the table and its RLS policy), which is not subject to the per-tenant RLS filter —
so the UPDATE reaches every firm's rows, not just a context-scoped subset. The
downgrade is intentionally a no-op: the original per-row counts are not recoverable
(and re-inflating them would restore the unenforced promise this closes).
"""

from alembic import op

revision = "0024_clamp_legacy_approvals"
down_revision = "0023_approval_matrix_rule"
branch_labels = None
depends_on = None

CLAMP = """
UPDATE approval_matrix_rule
   SET approvals_required = 1,
       updated_at = now()
 WHERE approvals_required > 1;
"""


def upgrade() -> None:
    op.execute(CLAMP)


def downgrade() -> None:
    # Irreversible by design: the pre-clamp counts are not retained, and restoring a
    # ``> 1`` value would re-create the unenforced control this migration removed.
    pass
