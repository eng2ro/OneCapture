"""Persist per-field OCR bounding boxes on the claim line (receipt highlighting)

Revision ID: 0013_ocr_boxes
Revises: 0012_audit_coding_fields
Create Date: 2026-06-29

The receipt viewer highlights where each captured field was read from. The vision
OCR returns a normalized box per field; we snapshot it on the line so re-rendering
the review screen needs no re-OCR.

  * ``claim_line.ocr_boxes`` jsonb — { field_name: [x, y, w, h] } normalized 0..1.

Additive, nullable. The box data is provider-agnostic (vision OCR now; a precise
document-AI provider can populate the same shape later).
"""

from alembic import op

revision = "0013_ocr_boxes"
down_revision = "0012_audit_coding_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE claim_line ADD COLUMN ocr_boxes jsonb;")


def downgrade() -> None:
    op.execute("ALTER TABLE claim_line DROP COLUMN IF EXISTS ocr_boxes;")
