"""Mileage claim lines — relax the line image NOT NULL (route, not receipt)

Revision ID: 0014_mileage_line
Revises: 0013_ocr_boxes
Create Date: 2026-06-29

A mileage claim line has no receipt image — its evidence is the route (from/to/
waypoints + distance), stored in the existing ``claim_line.mileage`` jsonb. So the
per-line ``image_path``/``image_sha256`` (NOT NULL since 0008, for receipt lines)
must become nullable. Receipt lines still populate them.

No new columns: the route lives in the existing ``mileage`` jsonb; ``quantity`` =
km and ``unit`` = 'km' carry the activity data forwarded to CarbonNext.
"""

from alembic import op

revision = "0014_mileage_line"
down_revision = "0013_ocr_boxes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE claim_line ALTER COLUMN image_path   DROP NOT NULL;")
    op.execute("ALTER TABLE claim_line ALTER COLUMN image_sha256 DROP NOT NULL;")


def downgrade() -> None:
    # Re-imposing NOT NULL would fail if mileage lines (NULL image) exist — the
    # correct guard. Receipt-only installs downgrade cleanly.
    op.execute("ALTER TABLE claim_line ALTER COLUMN image_sha256 SET NOT NULL;")
    op.execute("ALTER TABLE claim_line ALTER COLUMN image_path   SET NOT NULL;")
