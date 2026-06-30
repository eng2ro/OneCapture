"""Relax category uniqueness so staff e-claims can have a real category list

Revision ID: 0007_relax_category_unique
Revises: 0006_category_master
Create Date: 2026-06-26

0006 modelled the category master carbon-first: ``UNIQUE (client_id, expense_type)``
allowed exactly one category per OCR carbon key per client. That works for the
activity types (one Diesel, one Electricity) but collapses every ordinary staff
expense — meals, taxi, hotel, parking, stationery, mileage — into the single
``expense_type = 'other'`` bucket, so a client can have at most ONE spend-based
category. Staff e-claims need many.

This drops ``uq_category_client_expense`` and keeps ``uq_category_client_name``
(name stays the human-unique key per client). Claims now pick their category
directly (``claim.category_id``, added 0006); ``expense_type`` keeps feeding the
carbon factor for activity categories. No data change, no new columns — purely a
constraint relaxation. Downgrade re-adds the constraint (will fail if duplicate
expense_types now exist, which is the expected, correct guard).
"""

from alembic import op

revision = "0007_relax_category_unique"
down_revision = "0006_category_master"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE category DROP CONSTRAINT IF EXISTS uq_category_client_expense;"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE category ADD CONSTRAINT uq_category_client_expense "
        "UNIQUE (client_id, expense_type);"
    )
