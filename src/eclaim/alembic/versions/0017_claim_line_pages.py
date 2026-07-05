"""Claim line: constituent page images (merge/split)

Revision ID: 0017_claim_line_pages
Revises: 0016_claim_no_and_indexes
Create Date: 2026-07-01

A capture upload can be a multi-invoice PDF or a batch. When the per-client policy
``allow_document_split`` is on, a reviewer may MERGE lines that are really pages of
one invoice, or SPLIT a multi-page line back into separate invoices. A merged line
keeps its constituent page images in ``claim_line.pages`` — an ordered JSON list of
``{sha, path}`` — so it can be split again; ``image_path`` then holds the stitched
composite shown in the viewer. NULL (the default) = an ordinary single-image line.

Purely additive, nullable. Tenant table already carries RLS; a new column needs no
policy change.
"""

from alembic import op

revision = "0017_claim_line_pages"
down_revision = "0016_claim_no_and_indexes"
branch_labels = None
depends_on = None


UPGRADE = """
ALTER TABLE claim_line
  ADD COLUMN pages jsonb;
"""

DOWNGRADE = """
ALTER TABLE claim_line
  DROP COLUMN IF EXISTS pages;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
