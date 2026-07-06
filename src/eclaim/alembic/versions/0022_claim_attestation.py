"""Out-of-pocket attestation on the claim (Appendix A, Layer 1)

Revision ID: 0022_claim_attestation
Revises: 0021_release_batch_unique_hash
Create Date: 2026-07-06

To reimburse an out-of-pocket claim we need the employee to attest they actually
paid it themselves and haven't (and won't) be reimbursed elsewhere — a cheap,
high-value control that a card number can't provide. The web capture form now
requires a declaration checkbox to submit; we record WHO attested and WHEN so the
evidence pack and the reviewer can see it.

Both columns are nullable: existing claims, and the API/claimant-intake channel
(which has no interactive checkbox), simply carry NULL.
"""

from alembic import op

revision = "0022_claim_attestation"
down_revision = "0021_release_batch_unique_hash"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE claim ADD COLUMN attested_by text;
ALTER TABLE claim ADD COLUMN attested_at timestamptz;
"""

DOWNGRADE = """
ALTER TABLE claim DROP COLUMN IF EXISTS attested_at;
ALTER TABLE claim DROP COLUMN IF EXISTS attested_by;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
