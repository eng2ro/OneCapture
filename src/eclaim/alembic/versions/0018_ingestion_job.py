"""Async ingestion job queue for large multi-invoice uploads

Revision ID: 0018_ingestion_job
Revises: 0017_claim_line_pages
Create Date: 2026-07-02

A big multi-invoice PDF (e.g. 30 receipts in one file) is read one page at a time
by the vision model — too slow to do inside the /capture request without the
browser looking hung. This adds a durable job queue: /capture stages the raw
uploads and inserts an ``ingestion_job`` row; an in-process worker claims queued
jobs (``FOR UPDATE SKIP LOCKED``) and builds the claim in the background while the
page polls progress.

Tenant/RLS note: like every data table the job is firm+client scoped. But the
worker must CLAIM a queued job before it knows whose firm it is, so the policy
also admits a trusted worker context — ``current_setting('app.worker') = 'on'`` —
which only the in-process worker sets (server-side ``SET LOCAL``, never from user
input). All downstream claim/claim_line writes still run under the real tenant
context resolved from the claimed row, so they stay strictly scoped.
"""

from alembic import op

revision = "0018_ingestion_job"
down_revision = "0017_claim_line_pages"
branch_labels = None
depends_on = None

# nullif(..., '') guards the uuid cast: a SET LOCAL GUC reverts to '' (not NULL)
# after its transaction on a POOLED connection, and ''::uuid raises. The worker
# runs with no firm context, so without this guard the left operand would error
# before the `OR app.worker` could admit the row.
_FIRM_MATCH = "firm_id = nullif(current_setting('app.current_firm', true), '')::uuid"
_CLIENT_MATCH = (
    "client_id = ANY(string_to_array("
    "nullif(current_setting('app.allowed_clients', true), ''), ',')::uuid[])"
)
_WORKER = "current_setting('app.worker', true) = 'on'"
_POLICY = f"(({_FIRM_MATCH} AND {_CLIENT_MATCH}) OR {_WORKER})"

UPGRADE = f"""
CREATE TABLE ingestion_job (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id             uuid NOT NULL,
    client_id           uuid NOT NULL,
    created_by_user_id  uuid,
    claim_id            uuid,
    status              text NOT NULL DEFAULT 'queued',
    total_units         integer NOT NULL DEFAULT 0,
    done_units          integer NOT NULL DEFAULT 0,
    attempts            integer NOT NULL DEFAULT 0,
    error               text,
    payload             jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    heartbeat_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_ingestion_job_status
        CHECK (status IN ('queued','running','done','failed'))
);
CREATE INDEX ix_ingestion_job_queue ON ingestion_job (status, created_at);
CREATE INDEX ix_ingestion_job_client ON ingestion_job (client_id, created_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON ingestion_job TO onecapture_app;

ALTER TABLE ingestion_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_job FORCE ROW LEVEL SECURITY;
CREATE POLICY ingestion_job_tenant ON ingestion_job FOR ALL
    USING ({_POLICY}) WITH CHECK ({_POLICY});
"""

DOWNGRADE = """
DROP POLICY IF EXISTS ingestion_job_tenant ON ingestion_job;
ALTER TABLE ingestion_job NO FORCE ROW LEVEL SECURITY;
ALTER TABLE ingestion_job DISABLE ROW LEVEL SECURITY;
DROP TABLE IF EXISTS ingestion_job;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
