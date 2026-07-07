"""Parent-document reference on carbon_handoff: doc_no + doc_gross_total (F-B)

Revision ID: 0027_handoff_doc_ref
Revises: 0026_ap_domain
Create Date: 2026-07-07

The carbon unit is the LINE, never the document — a single bill can hold both
carbon-relevant and non-carbon lines, so the amount reaching CarbonNext is legitimately
LESS than the document total. To reconcile by REFERENCE (never by totals), every
forwarded/reversed handoff line now carries which document it came from (``doc_no``)
and that document's gross total (``doc_gross_total``). This lets CarbonNext and any
auditor answer "which bill is this, and why is it less than the invoice total?" and
powers the coverage view (captured spend vs carbon-forwarded).

The SAME two fields are the contract for the future AP handoff (``ap_invoice.doc_no`` +
``ap_invoice.total_amount``), so both channels reconcile identically.

Both columns are nullable: existing handoff rows (pre-F-B) simply carry NULL. The table
stays append-only (0019 revoked UPDATE/DELETE from onecapture_app); this is owner DDL.
"""

from alembic import op

revision = "0027_handoff_doc_ref"
down_revision = "0026_ap_domain"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE carbon_handoff ADD COLUMN doc_no text;
ALTER TABLE carbon_handoff ADD COLUMN doc_gross_total numeric(14,2);
"""

DOWNGRADE = """
ALTER TABLE carbon_handoff DROP COLUMN IF EXISTS doc_gross_total;
ALTER TABLE carbon_handoff DROP COLUMN IF EXISTS doc_no;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
