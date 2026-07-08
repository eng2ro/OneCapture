"""Per-client settings registry (owner request 2026-07-08: controls and
integrations must be configurable per company)

Revision ID: 0037_client_setting
Revises: 0036_handoff_travel_fields
Create Date: 2026-07-08

The Appendix-B rule made concrete: every behavioural control becomes a SETTING,
never a per-customer code branch. Key-value per client, validated against a
code-side registry (services/settings.py) that defines each key's allowed
values and default — the admin UI renders from the registry, so adding a new
control is one registry entry, no schema change. First tenants:
``carbon.auto_reverse`` (allow | approver_reason | off) and ``fx.auto_prefill``
(on | off); the CarbonNext reversal-disposition flags join here once their API
is confirmed (F-F). Integrity rules (SoD, append-only ledger, post-approval
lock) are deliberately NOT settable.
"""

from alembic import op

revision = "0037_client_setting"
down_revision = "0036_handoff_travel_fields"
branch_labels = None
depends_on = None

_FIRM_CAST = "nullif(current_setting('app.current_firm', true), '')::uuid"
_CLIENT_MATCH = (
    "client_id::text = ANY(string_to_array("
    "nullif(current_setting('app.allowed_clients', true), ''), ','))"
)
_DATA_POLICY = f"firm_id = {_FIRM_CAST} AND {_CLIENT_MATCH}"

UPGRADE = f"""
CREATE TABLE client_setting (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id     uuid NOT NULL REFERENCES firm(id),
  client_id   uuid NOT NULL REFERENCES client(id),
  key         text NOT NULL,
  value       text NOT NULL,
  updated_by  text NOT NULL DEFAULT '',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_client_setting UNIQUE (client_id, key)
);
CREATE INDEX ix_client_setting_firm_client ON client_setting(firm_id, client_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON client_setting TO onecapture_app;

ALTER TABLE client_setting ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_setting FORCE ROW LEVEL SECURITY;
CREATE POLICY client_setting_tenant ON client_setting FOR ALL
  USING ({_DATA_POLICY}) WITH CHECK ({_DATA_POLICY});
"""

DOWNGRADE = """
DROP POLICY IF EXISTS client_setting_tenant ON client_setting;
REVOKE SELECT, INSERT, UPDATE, DELETE ON client_setting FROM onecapture_app;
DROP TABLE IF EXISTS client_setting;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
