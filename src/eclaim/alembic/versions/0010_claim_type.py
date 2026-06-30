"""e-Claim claim-level purpose/type + claim date range (Submit redesign)

Revision ID: 0010_claim_type
Revises: 0009_carbon_handoff_per_line
Create Date: 2026-06-28

Gives a claim a compulsory **type/purpose** (general / travel / training /
client_meeting / other) so the approver gets instant context, plus an optional
claim-level **date range** (start_date / end_date).

The date range is required by the application — NOT the database — only for a
standalone claim whose type is not 'general' (a trip/training without an Event to
inherit dates from). The column stays nullable here because a claim that attaches
an Event inherits the event's dates and leaves these NULL; enforcing the
conditional rule in SQL would be brittle. See ``ClaimService.start_claim``.

PURELY ADDITIVE: ``claim_type`` defaults to 'general', so every existing claim
backfills to 'general' and nothing else changes. CHECK pins the small vocabulary.
"""

from alembic import op

revision = "0010_claim_type"
down_revision = "0009_carbon_handoff_per_line"
branch_labels = None
depends_on = None


UPGRADE = """
ALTER TABLE claim
  ADD COLUMN claim_type text NOT NULL DEFAULT 'general'
    CHECK (claim_type IN ('general','travel','training','client_meeting','other')),
  ADD COLUMN start_date date,
  ADD COLUMN end_date   date;
"""

DOWNGRADE = """
ALTER TABLE claim
  DROP COLUMN IF EXISTS end_date,
  DROP COLUMN IF EXISTS start_date,
  DROP COLUMN IF EXISTS claim_type;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
