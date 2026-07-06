"""Make a release content-addressed: UNIQUE(client_id, batch_hash) on release_batch

Revision ID: 0021_release_batch_unique_hash
Revises: 0020_claim_ingestion_job_link
Create Date: 2026-07-06

Two concurrent ``/release`` (or ``/reverse``) calls on the same claim could both
pass the in-Python idempotency checks and each write a ReleaseBatch — a double
release with a forked audit chain. A carbon claim was partly protected by the
handoff idempotency key, but a claim with NO carbon-relevant lines writes no
handoff, so nothing stopped it.

A release is content-addressed by its deterministic ``batch_hash`` (over the line
payloads, or the claim id when there are none). UNIQUE(client_id, batch_hash)
makes a second identical batch impossible at the database, so even if the service
lock were bypassed the double-release cannot persist; the service maps the
resulting IntegrityError to an idempotent no-op (returns the existing batch).

Both e-Claim and ERP Sync share this table and content-address the same way, so
the constraint holds for both. Reversal batches hash a distinct payload
(``reversal_of`` per line), so a forward release and its reversal never collide.
"""

from alembic import op

revision = "0021_release_batch_unique_hash"
down_revision = "0020_claim_ingestion_job_link"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE release_batch
    ADD CONSTRAINT uq_release_batch_client_hash UNIQUE (client_id, batch_hash);
"""

DOWNGRADE = """
ALTER TABLE release_batch DROP CONSTRAINT IF EXISTS uq_release_batch_client_hash;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
