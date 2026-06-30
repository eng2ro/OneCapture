"""Audit-grade claim coding — posting date, supplier tax id, department, project

Revision ID: 0012_audit_coding_fields
Revises: 0011_carbon_relevant_handoff
Create Date: 2026-06-28

For a listed company every claim line is an accounting source document. Most of
the coding columns already exist on ``claim_line`` (gl_code, cost_centre_override,
tax_amount, tax_code, tax_inclusive, net_amount, fx_rate, base_amount) — they were
just not surfaced. This migration adds the few that were genuinely missing so a
line can be posted and audited:

  * ``claim_line.posting_date``     date  — the accounting/posting date, distinct
                                            from the vendor invoice date (doc_date)
  * ``claim_line.supplier_tax_id``  text  — vendor SST/GST registration no (input-
                                            tax credit audit)
  * ``claim_line.department``       text  — cost dimension (line override)
  * ``claim_line.project_code``     text  — cost dimension (line override)
  * ``claim.department`` / ``claim.project_code`` — claim-level defaults for a
                                            standalone claim (no event to inherit).

Purely additive, all nullable. Tenant tables already carry RLS; adding columns
needs no policy change.
"""

from alembic import op

revision = "0012_audit_coding_fields"
down_revision = "0011_carbon_relevant_handoff"
branch_labels = None
depends_on = None


UPGRADE = """
ALTER TABLE claim_line
  ADD COLUMN posting_date    date,
  ADD COLUMN supplier_tax_id text,
  ADD COLUMN department      text,
  ADD COLUMN project_code    text;

ALTER TABLE claim
  ADD COLUMN department   text,
  ADD COLUMN project_code text;
"""

DOWNGRADE = """
ALTER TABLE claim
  DROP COLUMN IF EXISTS project_code,
  DROP COLUMN IF EXISTS department;

ALTER TABLE claim_line
  DROP COLUMN IF EXISTS project_code,
  DROP COLUMN IF EXISTS department,
  DROP COLUMN IF EXISTS supplier_tax_id,
  DROP COLUMN IF EXISTS posting_date;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
