"""Allow quotation + purchase_order document types on document_intake

Revision ID: 0030_intake_doc_types
Revises: 0029_matrix_scope_backfill
Create Date: 2026-07-07

The classifier now distinguishes a supplier ``quotation`` (a price offer — NOT
payable) and a ``purchase_order`` (an order — not itself a bill) from a payable
``vendor_invoice``, so proposals stop being mislabelled as invoices and offered "File
as AP invoice". Widen the intake document_type CHECK to admit the two new values;
existing rows are unaffected. Reversible (the new values just weren't used before).
"""

from alembic import op

revision = "0030_intake_doc_types"
down_revision = "0029_matrix_scope_backfill"
branch_labels = None
depends_on = None

_NEW = (
    "document_type IN ('expense_receipt','vendor_invoice','delivery_order',"
    "'quotation','purchase_order','unknown')"
)
_OLD = (
    "document_type IN ('expense_receipt','vendor_invoice','delivery_order','unknown')"
)


def upgrade() -> None:
    op.execute("ALTER TABLE document_intake DROP CONSTRAINT ck_document_intake_type")
    op.execute(f"ALTER TABLE document_intake ADD CONSTRAINT ck_document_intake_type CHECK ({_NEW})")


def downgrade() -> None:
    op.execute("ALTER TABLE document_intake DROP CONSTRAINT ck_document_intake_type")
    op.execute(f"ALTER TABLE document_intake ADD CONSTRAINT ck_document_intake_type CHECK ({_OLD})")
