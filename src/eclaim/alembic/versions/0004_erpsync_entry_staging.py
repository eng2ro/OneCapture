"""ERP Sync per-line staging table (erpsync_entry) + RLS

Revision ID: 0004_erpsync_entry_staging
Revises: 0003_harden_firm_rls
Create Date: 2026-06-20

Adds ``erpsync_entry`` — the rich staging table for EVERY accepted ERP Sync AP
line, carrying a review ``status`` (clean / held / flagged / released). It is to
ERP Sync what ``claim`` is to e-Claim: the reviewable row that a later release
projects into the shared ``emission_entry`` ledger. Staged held rows (cross-
channel dedup) and flagged rows (unmapped / spend-based / DQ) live here
distinguished by ``status`` — there is deliberately NO separate held-rows table.
Malformed (REJECTED) lines never reach this table; they stay report-only.

Tenant-scoped (firm_id/client_id) and RLS ENABLE+FORCE with the SAME hardened
policy expression as the other data tables (0003): an empty ``app.current_firm``
resolves to NULL → deny, never a ``''::uuid`` cast error.

Runs as the admin/owner (superuser), which bypasses RLS, so the CREATE + GRANT
succeed before the policy bites for the unprivileged ``onecapture_app`` role.
"""

from alembic import op

revision = "0004_erpsync_entry_staging"
down_revision = "0003_harden_firm_rls"
branch_labels = None
depends_on = None

# RLS expressions — byte-identical to 0003's hardened data-table policy so
# erpsync_entry is isolated exactly like claim / emission_entry / etc.
_FIRM_CAST = "nullif(current_setting('app.current_firm', true), '')::uuid"
_CLIENT_MATCH = (
    "(client_id IS NULL OR client_id = ANY("
    "string_to_array(nullif(current_setting('app.allowed_clients', true), ''), ',')::uuid[]))"
)
_DATA_POLICY = f"firm_id = {_FIRM_CAST} AND {_CLIENT_MATCH}"


CREATE_TABLE = """
CREATE TABLE erpsync_entry (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id         uuid NOT NULL REFERENCES firm(id),
  client_id       uuid NOT NULL REFERENCES client(id),

  -- Source line identity: the (client_id, doc_entry, line_num) idempotency grain.
  doc_entry       text NOT NULL,
  line_num        integer NOT NULL,
  doc_number      text,

  -- Carbon classification result (mirrors erpsync.domain.models.EmissionEntry).
  category        text NOT NULL,
  scope           text NOT NULL
                  CHECK (scope IN ('scope_1','scope_2','scope_3_4','scope_3_11','scope_3_other')),
  basis           text NOT NULL CHECK (basis IN ('activity','spend')),
  data_quality    text NOT NULL CHECK (data_quality IN ('measured','estimated','flagged')),
  quantity        numeric(14,4),
  uom             text,
  amount          numeric(14,2),
  factor_ref      text NOT NULL DEFAULT '',
  factor_value    numeric(18,6) NOT NULL,
  factor_version  text NOT NULL,
  rule_id         text NOT NULL DEFAULT '',
  rule_version    text NOT NULL,
  tco2e           numeric(18,6) NOT NULL,
  source_hash     text NOT NULL,
  notes           jsonb,

  -- Review state: clean (mapped + measured), held (cross-channel dedup),
  -- flagged (unmapped / spend-based / DQ), released (projected to the ledger).
  status          text NOT NULL CHECK (status IN ('clean','held','flagged','released')),
  created_at      timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_erpsync_entry_line UNIQUE (client_id, doc_entry, line_num)
);
CREATE INDEX ix_erpsync_entry_firm ON erpsync_entry(firm_id);
CREATE INDEX ix_erpsync_entry_client_status ON erpsync_entry(client_id, status);

-- onecapture_app already inherits DML on owner-created tables via 0002's
-- ALTER DEFAULT PRIVILEGES (this migration runs as that same owner), but grant
-- explicitly too so the table's access is self-documenting and robust.
GRANT SELECT, INSERT, UPDATE, DELETE ON erpsync_entry TO onecapture_app;
"""

RLS = f"""
ALTER TABLE erpsync_entry ENABLE ROW LEVEL SECURITY;
ALTER TABLE erpsync_entry FORCE ROW LEVEL SECURITY;
CREATE POLICY erpsync_entry_tenant ON erpsync_entry FOR ALL
  USING ({_DATA_POLICY}) WITH CHECK ({_DATA_POLICY});
"""

DOWNGRADE = """
DROP POLICY IF EXISTS erpsync_entry_tenant ON erpsync_entry;
REVOKE SELECT, INSERT, UPDATE, DELETE ON erpsync_entry FROM onecapture_app;
DROP TABLE IF EXISTS erpsync_entry;
"""


def upgrade() -> None:
    op.execute(CREATE_TABLE)
    op.execute(RLS)


def downgrade() -> None:
    op.execute(DOWNGRADE)
