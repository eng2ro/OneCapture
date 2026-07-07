"""Backfill approval_matrix_rule.scope_module NULL → 'eclaim' (F7)

Revision ID: 0029_matrix_scope_backfill
Revises: 0028_intake_job_link
Create Date: 2026-07-07

C2 added ``scope_module`` (NULL = every module). But every EXISTING rule was written
before AP existed, with NULL — so an e-Claim approval matrix silently began governing
AP invoice approvals too, invisibly to the admin (the UI can neither show nor set the
scope). Clamp existing NULL rows to ``'eclaim'`` so they govern ONLY e-Claim, as their
authors intended; the admin UI now writes ``'eclaim'`` explicitly and shows the column,
and an AP matrix is configured deliberately (scope_module='ap'). AP approval falls back
to its SoD + authority-limit gate until such a rule exists.

Idempotent and safe to re-run. The downgrade is a no-op (the original NULLs are not
recoverable, and re-NULLing would restore the silent cross-module governance).
"""

from alembic import op

revision = "0029_matrix_scope_backfill"
down_revision = "0028_intake_job_link"
branch_labels = None
depends_on = None

BACKFILL = """
UPDATE approval_matrix_rule
   SET scope_module = 'eclaim', updated_at = now()
 WHERE scope_module IS NULL;
"""


def upgrade() -> None:
    op.execute(BACKFILL)


def downgrade() -> None:
    pass
