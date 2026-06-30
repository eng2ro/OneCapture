"""e-Claim becomes carbon-free: per-category relevance flag + raw CarbonNext handoff

Revision ID: 0011_carbon_relevant_handoff
Revises: 0010_claim_type
Create Date: 2026-06-28

e-Claim stops doing ANY carbon classification. It is pure staff-claim handling:
capture -> categorize -> approve -> ERP export. The ONLY carbon metadata it keeps
is a per-category boolean ``carbon_relevant`` (= "send this to CarbonNext?"). For
the relevant lines it forwards RAW expense data; CarbonNext owns scope, factors and
tonnage.

Changes (additive — nothing dropped, legacy carbon columns kept vestigial):
  * ``category.carbon_relevant``   bool  (backfill: carbon_class <> 'none')
  * ``claim_line.carbon_relevant`` bool  (backfill: carbon_class IN direct/spend)
  * NEW ``carbon_handoff`` — one row per forwarded relevant line, RAW fields only
    (category, amount, currency, quantity, unit, vendor, date, cost_centre) + the
    release batch + idempotency. This REPLACES e-Claim's use of the shared
    ``emission_entry`` ledger, which requires scope/factor e-Claim no longer has.

CRITICAL: ``emission_entry`` / ``emission_factor`` are SHARED with ERP Sync, which
still computes real tonnage and writes them. This migration does NOT touch those
tables — it only stops e-Claim from writing ``emission_entry`` (a code change) and
gives e-Claim its own raw handoff table. ERP Sync is unaffected.

Tenant-scoped + RLS with the same hardened firm/client policy as the other e-Claim
data tables (0006/0008 cast).
"""

from alembic import op

revision = "0011_carbon_relevant_handoff"
down_revision = "0010_claim_type"
branch_labels = None
depends_on = None

_FIRM_CAST = "nullif(current_setting('app.current_firm', true), '')::uuid"
_CLIENT_MATCH = (
    "(client_id IS NULL OR client_id = ANY("
    "string_to_array(nullif(current_setting('app.allowed_clients', true), ''), ',')::uuid[]))"
)
_DATA_POLICY = f"firm_id = {_FIRM_CAST} AND {_CLIENT_MATCH}"


RELEVANCE_FLAGS = """
ALTER TABLE category ADD COLUMN carbon_relevant boolean NOT NULL DEFAULT true;
UPDATE category SET carbon_relevant = (carbon_class <> 'none');

ALTER TABLE claim_line ADD COLUMN carbon_relevant boolean NOT NULL DEFAULT true;
UPDATE claim_line SET carbon_relevant = (carbon_class IN ('direct','spend'));
"""

# Raw forwarded payload. NO scope/factor/basis/tco2e — that is CarbonNext's job.
# ``direction`` distinguishes a normal forward from a reversal (correction).
CREATE_HANDOFF = """
CREATE TABLE carbon_handoff (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id          uuid NOT NULL REFERENCES firm(id),
  client_id        uuid NOT NULL REFERENCES client(id),
  claim_id         uuid NOT NULL REFERENCES claim(id),
  line_id          uuid NOT NULL REFERENCES claim_line(id),
  release_batch_id uuid NOT NULL REFERENCES release_batch(id),

  category_id      uuid REFERENCES category(id),
  category_name    text,
  expense_type     text,
  vendor           text,
  doc_date         text,
  amount           numeric(14,2),
  currency         text,
  quantity         numeric(14,4),
  unit             text,
  cost_centre      text,

  direction        text NOT NULL DEFAULT 'forward'
                   CHECK (direction IN ('forward','reversal')),
  idempotency_key  text NOT NULL,
  carbon_ref       text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_carbon_handoff_idem UNIQUE (idempotency_key)
);
CREATE INDEX ix_carbon_handoff_client ON carbon_handoff(client_id);
CREATE INDEX ix_carbon_handoff_firm ON carbon_handoff(firm_id);
CREATE INDEX ix_carbon_handoff_batch ON carbon_handoff(release_batch_id);
GRANT SELECT, INSERT, UPDATE, DELETE ON carbon_handoff TO onecapture_app;
"""

RLS = f"""
ALTER TABLE carbon_handoff ENABLE ROW LEVEL SECURITY;
ALTER TABLE carbon_handoff FORCE ROW LEVEL SECURITY;
CREATE POLICY carbon_handoff_tenant ON carbon_handoff FOR ALL
  USING ({_DATA_POLICY}) WITH CHECK ({_DATA_POLICY});
"""

DOWNGRADE = """
DROP POLICY IF EXISTS carbon_handoff_tenant ON carbon_handoff;
REVOKE SELECT, INSERT, UPDATE, DELETE ON carbon_handoff FROM onecapture_app;
DROP TABLE IF EXISTS carbon_handoff;
ALTER TABLE claim_line DROP COLUMN IF EXISTS carbon_relevant;
ALTER TABLE category DROP COLUMN IF EXISTS carbon_relevant;
"""


def upgrade() -> None:
    op.execute(RELEVANCE_FLAGS)
    op.execute(CREATE_HANDOFF)
    op.execute(RLS)


def downgrade() -> None:
    op.execute(DOWNGRADE)
