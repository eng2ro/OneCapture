"""Key each async-built claim to its ingestion job — idempotent completion (B3)

Revision ID: 0020_claim_ingestion_job_link
Revises: 0019_ledger_immutability
Create Date: 2026-07-05

Async capture builds the claim in one transaction and marks the job ``done`` in
another. If the worker dies between those two commits the job is left ``running``,
its heartbeat goes stale, and it is re-claimed — building a SECOND claim for the
same upload (duplicate money + carbon).

Give ``claim`` an optional ``ingestion_job_id`` with a UNIQUE index: a claim built
from a job carries that job's id, so a retry can at most collide (blocked by the
constraint), never duplicate. The worker also checks for an existing claim before
rebuilding, so a normal crash-recovery just re-marks the job done without a second
build (and without re-billing OCR).

The column is NULL for the inline/interactive capture path. A UNIQUE index on a
nullable column permits many NULLs (Postgres treats NULLs as distinct), so inline
claims never collide with each other.
"""

from alembic import op

revision = "0020_claim_ingestion_job_link"
down_revision = "0019_ledger_immutability"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE claim ADD COLUMN ingestion_job_id uuid;
CREATE UNIQUE INDEX uq_claim_ingestion_job ON claim (ingestion_job_id);
"""

DOWNGRADE = """
DROP INDEX IF EXISTS uq_claim_ingestion_job;
ALTER TABLE claim DROP COLUMN IF EXISTS ingestion_job_id;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
