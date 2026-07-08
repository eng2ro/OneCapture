"""Cat-6 business-travel fields on the carbon handoff (Appendix G/H, F-D contract)

Revision ID: 0036_handoff_travel_fields
Revises: 0035_vehicle_registry
Create Date: 2026-07-07

CarbonNext's Scope 3 Category 6 (Business Travel) spec requires the employee
(id/name/department/position), the travel purpose, and — for the distance-based
method — the vehicle type per record. All of these were already CAPTURED
(claimant master, claim purpose, vehicle registry) but never forwarded. All
nullable: non-travel lines and claimant-less (firm-keyed) claims simply carry
NULLs. ``department`` already forwards (0032); ``position`` rides employee_ref/
name's source row and can join later if CarbonNext wants it split out.
"""

from alembic import op

revision = "0036_handoff_travel_fields"
down_revision = "0035_vehicle_registry"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE carbon_handoff
  ADD COLUMN employee_ref   text,
  ADD COLUMN employee_name  text,
  ADD COLUMN position       text,
  ADD COLUMN travel_purpose text,
  ADD COLUMN vehicle_type   text;
"""

DOWNGRADE = """
ALTER TABLE carbon_handoff
  DROP COLUMN vehicle_type,
  DROP COLUMN travel_purpose,
  DROP COLUMN position,
  DROP COLUMN employee_name,
  DROP COLUMN employee_ref;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
