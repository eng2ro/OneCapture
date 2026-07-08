"""Carbon handoff payload fields: net/tax/base amounts + department (F-D contract)

Revision ID: 0032_handoff_payload_fields
Revises: 0031_ap_submitter_sod
Create Date: 2026-07-07

The F-D field contract (and the F-C promise to CarbonNext: "NET or GROSS — we can
send both; you choose") requires the handoff to carry the net (ex-tax) amount, the
tax itself, and the MYR base value of a foreign receipt — all three were captured,
stored and human-verified on claim_line but then DROPPED at the forward step.
``department`` likewise never forwarded, making departmental emission attribution
impossible downstream. All nullable; existing rows simply have no values (they
predate the contract).
"""

from alembic import op

revision = "0032_handoff_payload_fields"
down_revision = "0031_ap_submitter_sod"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE carbon_handoff
  ADD COLUMN net_amount   numeric(14,2),
  ADD COLUMN tax_amount   numeric(14,2),
  ADD COLUMN base_amount  numeric(14,2),
  ADD COLUMN department   text;
"""

DOWNGRADE = """
ALTER TABLE carbon_handoff
  DROP COLUMN department,
  DROP COLUMN base_amount,
  DROP COLUMN tax_amount,
  DROP COLUMN net_amount;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
