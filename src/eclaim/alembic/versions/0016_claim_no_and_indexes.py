"""Human-readable claim number + scale/integrity constraints

Revision ID: 0016_claim_no_and_indexes
Revises: 0015_claim_header
Create Date: 2026-06-29

Two data-model improvements from the head-to-tail audit:

  * ``claim.claim_no`` text — a stable, human-readable reference (``CLM-2026-000123``)
    that finance, auditors and payment files can quote, instead of a raw UUID. Drawn
    from a Postgres SEQUENCE (atomic, concurrency-safe); existing rows are backfilled
    in created_at order. UNIQUE.
  * Scale + integrity:
      - UNIQUE (claim_id, line_no) on claim_line — closes the read-then-insert race
        in ``next_line_no`` that could mint duplicate line numbers.
      - index claim (client_id, created_at DESC) — every inbox/export sorts on this.
      - index carbon_handoff (line_id) — the per-release idempotency lookup filters it.

All additive. Tenant tables already carry RLS; new columns/indexes need no policy
change. The number sequence is global (not per-firm-contiguous) — unique + readable,
which is what matters; a per-firm/per-year reset can come later if a tenant needs it.
"""

from alembic import op

revision = "0016_claim_no_and_indexes"
down_revision = "0015_claim_header"
branch_labels = None
depends_on = None


UPGRADE = """
CREATE SEQUENCE IF NOT EXISTS claim_no_seq;

ALTER TABLE claim ADD COLUMN claim_no text;

-- Backfill existing rows chronologically as CLM-<year>-<6-digit running no>.
WITH ordered AS (
  SELECT id,
         to_char(created_at, 'YYYY') AS yr,
         row_number() OVER (ORDER BY created_at, id) AS rn
  FROM claim
)
UPDATE claim c
SET claim_no = 'CLM-' || o.yr || '-' || lpad(o.rn::text, 6, '0')
FROM ordered o
WHERE c.id = o.id;

-- Advance the sequence past what we just assigned, so new claims continue cleanly.
SELECT setval('claim_no_seq', GREATEST((SELECT count(*) FROM claim), 1));

ALTER TABLE claim ADD CONSTRAINT uq_claim_no UNIQUE (claim_no);

ALTER TABLE claim_line ADD CONSTRAINT uq_claim_line_no UNIQUE (claim_id, line_no);

CREATE INDEX ix_claim_client_created ON claim (client_id, created_at DESC);
CREATE INDEX ix_carbon_handoff_line ON carbon_handoff (line_id);
"""

DOWNGRADE = """
DROP INDEX IF EXISTS ix_carbon_handoff_line;
DROP INDEX IF EXISTS ix_claim_client_created;
ALTER TABLE claim_line DROP CONSTRAINT IF EXISTS uq_claim_line_no;
ALTER TABLE claim DROP CONSTRAINT IF EXISTS uq_claim_no;
ALTER TABLE claim DROP COLUMN IF EXISTS claim_no;
DROP SEQUENCE IF EXISTS claim_no_seq;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
