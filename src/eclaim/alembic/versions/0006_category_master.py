"""e-Claim category master (FR-E?: expense_type → factor mapping) + claim.category_id

Revision ID: 0006_category_master
Revises: 0005_erpsync_review_lifecycle
Create Date: 2026-06-22

Adds a per-client ``category`` master — the reviewable mapping from an OCR
``expense_type`` to an emission factor (``factor_key``; NULL = spend-based) plus
GL export + a default limit. It deliberately has NO ``scope`` column: scope stays
derived from the resolved factor exactly as ``services/classify.py`` does it today,
so the two can never drift out of step.

Also adds the additive, nullable ``claim.category_id`` FK so a claim can later
reference the category it was classified under. Purely additive: no data backfill,
no NOT NULL, no change to classification — wiring classify to read the master is a
separate follow-up.

Tenant-scoped (firm_id/client_id) and RLS ENABLE+FORCE with the SAME hardened
policy as the other e-Claim data tables (claim/claimant/release_batch/
emission_entry/audit_event): an empty ``app.current_firm`` resolves to NULL → deny
(0003), never a ``''::uuid`` cast error. Runs as the admin/owner (bypasses RLS),
so CREATE + GRANT + policy all apply before the policy bites for onecapture_app.
"""

from alembic import op

revision = "0006_category_master"
down_revision = "0005_erpsync_review_lifecycle"
branch_labels = None
depends_on = None

# Byte-identical to the post-0003 data-table policy on claim / claimant, so the
# category master is isolated exactly like every other tenant data table.
_FIRM_CAST = "nullif(current_setting('app.current_firm', true), '')::uuid"
_CLIENT_MATCH = (
    "(client_id IS NULL OR client_id = ANY("
    "string_to_array(nullif(current_setting('app.allowed_clients', true), ''), ',')::uuid[]))"
)
_DATA_POLICY = f"firm_id = {_FIRM_CAST} AND {_CLIENT_MATCH}"


CREATE_TABLE = """
CREATE TABLE category (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id         uuid NOT NULL REFERENCES firm(id),
  client_id       uuid NOT NULL REFERENCES client(id),

  name            text NOT NULL,
  expense_type    text NOT NULL,           -- the OCR expense_type this category maps
  factor_key      text,                    -- EF reference; NULL = spend-based
  gl_export_code  text,
  default_limit   numeric(14,2),           -- money limit, matching authority_limit's precision
  status          text NOT NULL DEFAULT 'active',
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),

  -- One category per (client, OCR key) and per (client, name).
  CONSTRAINT uq_category_client_expense UNIQUE (client_id, expense_type),
  CONSTRAINT uq_category_client_name    UNIQUE (client_id, name)
);
CREATE INDEX ix_category_firm_client ON category(firm_id, client_id);

-- onecapture_app already inherits DML on owner-created tables via 0002's
-- ALTER DEFAULT PRIVILEGES (this migration runs as that owner), but grant
-- explicitly too so the table's access is self-documenting and robust.
GRANT SELECT, INSERT, UPDATE, DELETE ON category TO onecapture_app;
"""

RLS = f"""
ALTER TABLE category ENABLE ROW LEVEL SECURITY;
ALTER TABLE category FORCE ROW LEVEL SECURITY;
CREATE POLICY category_tenant ON category FOR ALL
  USING ({_DATA_POLICY}) WITH CHECK ({_DATA_POLICY});
"""

# Additive, nullable FK — created AFTER category exists. No backfill.
CLAIM_FK = """
ALTER TABLE claim ADD COLUMN category_id uuid REFERENCES category(id);
"""

DOWNGRADE = """
ALTER TABLE claim DROP COLUMN IF EXISTS category_id;
DROP POLICY IF EXISTS category_tenant ON category;
REVOKE SELECT, INSERT, UPDATE, DELETE ON category FROM onecapture_app;
DROP TABLE IF EXISTS category;
"""


def upgrade() -> None:
    op.execute(CREATE_TABLE)
    op.execute(RLS)
    op.execute(CLAIM_FK)


def downgrade() -> None:
    op.execute(DOWNGRADE)
