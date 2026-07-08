"""Exchange-rate table (Appendix G-C): monthly currency → MYR rates per client

Revision ID: 0034_exchange_rate
Revises: 0033_ap_carbon_readiness
Create Date: 2026-07-07

Every CarbonNext Scope-3 spend field is denominated in MYR, so foreign
transactions must convert before posting. The rate a line uses resolves as:
human-entered fx_rate on the line (wins, audited) → this table's rate for the
document's month → none (the line is flagged "needs FX" and release notes it).
``source`` records where a rate came from: manual admin entry today; the ERP
connector's ``pull_fx_rates`` and a CarbonNext pull are seams for later —
whoever owns the rate must be the single source of truth (F-C question).
"""

from alembic import op

revision = "0034_exchange_rate"
down_revision = "0033_ap_carbon_readiness"
branch_labels = None
depends_on = None

_FIRM_CAST = "nullif(current_setting('app.current_firm', true), '')::uuid"
_CLIENT_MATCH = (
    "client_id::text = ANY(string_to_array("
    "nullif(current_setting('app.allowed_clients', true), ''), ','))"
)
_DATA_POLICY = f"firm_id = {_FIRM_CAST} AND {_CLIENT_MATCH}"

CREATE_TABLE = """
CREATE TABLE exchange_rate (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id      uuid NOT NULL REFERENCES firm(id),
  client_id    uuid NOT NULL REFERENCES client(id),

  currency     text NOT NULL,                 -- ISO-4217, e.g. USD
  period       date NOT NULL,                 -- month bucket (first of month)
  rate_to_myr  numeric(18,6) NOT NULL,        -- 1 unit of currency = X MYR
  source       text NOT NULL DEFAULT 'manual',
  created_by   text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_fx_source CHECK (source IN ('manual','erp','carbonnext')),
  CONSTRAINT ck_fx_rate_positive CHECK (rate_to_myr > 0),
  CONSTRAINT ck_fx_period_month CHECK (period = date_trunc('month', period)::date),
  CONSTRAINT uq_fx_client_ccy_period UNIQUE (client_id, currency, period)
);
CREATE INDEX ix_fx_firm_client ON exchange_rate(firm_id, client_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON exchange_rate TO onecapture_app;
"""

RLS = f"""
ALTER TABLE exchange_rate ENABLE ROW LEVEL SECURITY;
ALTER TABLE exchange_rate FORCE ROW LEVEL SECURITY;
CREATE POLICY exchange_rate_tenant ON exchange_rate FOR ALL
  USING ({_DATA_POLICY}) WITH CHECK ({_DATA_POLICY});
"""

DOWNGRADE = """
DROP POLICY IF EXISTS exchange_rate_tenant ON exchange_rate;
REVOKE SELECT, INSERT, UPDATE, DELETE ON exchange_rate FROM onecapture_app;
DROP TABLE IF EXISTS exchange_rate;
"""


def upgrade() -> None:
    op.execute(CREATE_TABLE)
    op.execute(RLS)


def downgrade() -> None:
    op.execute(DOWNGRADE)
