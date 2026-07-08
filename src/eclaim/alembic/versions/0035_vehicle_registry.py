"""Vehicle registry + claimant profile fields (Appendix H-B / H-C)

Revision ID: 0035_vehicle_registry
Revises: 0034_exchange_rate
Create Date: 2026-07-07

CarbonNext's distance-based method (Scope 3 Cat 4/6) needs a VEHICLE TYPE per
trip. Vehicles are a client-scoped REGISTRY, deliberately not a field on the
user: a claimant can claim on behalf of someone else (or paid in advance), so a
mileage line picks any registered vehicle — defaulting to the claimant's usual
one (``usual_claimant_id``). ``claimant`` gains position/department (the Cat-6
employee fields the specs require), and ``claim_line`` gains the vehicle FK.
"""

from alembic import op

revision = "0035_vehicle_registry"
down_revision = "0034_exchange_rate"
branch_labels = None
depends_on = None

_FIRM_CAST = "nullif(current_setting('app.current_firm', true), '')::uuid"
_CLIENT_MATCH = (
    "client_id::text = ANY(string_to_array("
    "nullif(current_setting('app.allowed_clients', true), ''), ','))"
)
_DATA_POLICY = f"firm_id = {_FIRM_CAST} AND {_CLIENT_MATCH}"

UPGRADE = f"""
CREATE TABLE vehicle (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id            uuid NOT NULL REFERENCES firm(id),
  client_id          uuid NOT NULL REFERENCES client(id),

  label              text NOT NULL,              -- plate or friendly name
  vehicle_type       text NOT NULL,
  engine_size        text,                       -- optional, e.g. "1.6L"
  usual_claimant_id  uuid REFERENCES claimant(id),
  active             boolean NOT NULL DEFAULT true,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_vehicle_type CHECK (vehicle_type IN (
    'car_petrol','car_diesel','car_hybrid','car_ev',
    'motorcycle','van','truck','other')),
  CONSTRAINT uq_vehicle_client_label UNIQUE (client_id, label)
);
CREATE INDEX ix_vehicle_firm_client ON vehicle(firm_id, client_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON vehicle TO onecapture_app;

ALTER TABLE vehicle ENABLE ROW LEVEL SECURITY;
ALTER TABLE vehicle FORCE ROW LEVEL SECURITY;
CREATE POLICY vehicle_tenant ON vehicle FOR ALL
  USING ({_DATA_POLICY}) WITH CHECK ({_DATA_POLICY});

ALTER TABLE claimant
  ADD COLUMN position   text,
  ADD COLUMN department text;

ALTER TABLE claim_line
  ADD COLUMN vehicle_id uuid REFERENCES vehicle(id);
"""

DOWNGRADE = """
ALTER TABLE claim_line DROP COLUMN vehicle_id;
ALTER TABLE claimant DROP COLUMN department, DROP COLUMN position;
DROP POLICY IF EXISTS vehicle_tenant ON vehicle;
REVOKE SELECT, INSERT, UPDATE, DELETE ON vehicle FROM onecapture_app;
DROP TABLE IF EXISTS vehicle;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
