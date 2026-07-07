"""Key diverted intake rows to their ingestion job — no retry duplicates (F3)

Revision ID: 0028_intake_job_link
Revises: 0027_handoff_doc_ref
Create Date: 2026-07-07

Same bug class as B3 (duplicate claims on retry): the async worker can re-run a job
(stale-running reclaim, or a crash before completion), and an all-diverted job — one
whose pages all went to the intake holding queue — leaves no claim for the worker's
idempotency check to key on, so it rebuilt and DOUBLE-recorded the vendor bills.

This adds ``document_intake.ingestion_job_id`` (nullable; inline captures leave it
NULL) plus a PARTIAL unique index on (ingestion_job_id, image_sha256) so the same page
cannot be diverted twice for the same job — the DB backstop. The worker also learns to
treat a job that already produced intake rows as done (see ingest/worker.py). Inline
captures (NULL job) are unconstrained — there is no retry there to guard against.
"""

from alembic import op

revision = "0028_intake_job_link"
down_revision = "0027_handoff_doc_ref"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE document_intake ADD COLUMN ingestion_job_id uuid;
CREATE INDEX ix_document_intake_job ON document_intake(ingestion_job_id);
CREATE UNIQUE INDEX uq_document_intake_job_sha
  ON document_intake(ingestion_job_id, image_sha256)
  WHERE ingestion_job_id IS NOT NULL;
"""

DOWNGRADE = """
DROP INDEX IF EXISTS uq_document_intake_job_sha;
DROP INDEX IF EXISTS ix_document_intake_job;
ALTER TABLE document_intake DROP COLUMN IF EXISTS ingestion_job_id;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
