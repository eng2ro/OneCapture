"""Claim header: document posting date + remarks

Revision ID: 0015_claim_header_posting_remarks
Revises: 0014_mileage_line
Create Date: 2026-06-29

World-standard expense systems (SAP Concur, Coupa, Workday) treat a claim as a
*document header* that groups N line items. The header carries the grouping
context the approver and the ERP read first. Two of those header fields were
missing:

  * ``claim.posting_date``  date  — the accounting/posting date for the whole
                                    document (one per claim, SAP B1-style). This
                                    is distinct from the per-line ``claim_line.
                                    posting_date`` override (added in 0012) and
                                    from each receipt's vendor ``doc_date``.
  * ``claim.remarks``       text  — free-text commentary on the claim. Mirrors
                                    Concur's *Comment* (free text, NOT posted to
                                    the ERP) as opposed to ``purpose`` which is
                                    the *Business Purpose* (the audit
                                    justification that DOES post). Distinct from
                                    ``approver_note`` (the reviewer's decision
                                    note).

Purely additive, both nullable. Tenant tables already carry RLS; adding columns
needs no policy change.
"""

from alembic import op

revision = "0015_claim_header"
down_revision = "0014_mileage_line"
branch_labels = None
depends_on = None


UPGRADE = """
ALTER TABLE claim
  ADD COLUMN posting_date date,
  ADD COLUMN remarks      text;
"""

DOWNGRADE = """
ALTER TABLE claim
  DROP COLUMN IF EXISTS remarks,
  DROP COLUMN IF EXISTS posting_date;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
