"""Approval authority matrix (Appendix B) — configurable sign-off rules

Revision ID: 0023_approval_matrix_rule
Revises: 0022_claim_attestation
Create Date: 2026-07-06

One tenant-scoped table drives who may approve a claim, by amount band (and,
future-proof, by department / category and multi-step chains). The launch engine
reads only ``step_order = 1`` — one approval per band; extra ``step_order`` rows
(multi-layer) and per-scope overrides are Phase-2 and need NO schema change, just
more rows. ``AppUser.authority_limit`` stays as an optional personal cap on top.

Tenant-scoped (firm_id/client_id) with the SAME hardened RLS policy as the other
e-Claim data tables (empty ``app.current_firm`` → NULL → deny, never a cast
error). Runs as the admin/owner so CREATE + GRANT + policy all apply before the
policy bites for onecapture_app.
"""

from alembic import op

revision = "0023_approval_matrix_rule"
down_revision = "0022_claim_attestation"
branch_labels = None
depends_on = None

_FIRM_CAST = "nullif(current_setting('app.current_firm', true), '')::uuid"
_CLIENT_MATCH = (
    "(client_id IS NULL OR client_id = ANY("
    "string_to_array(nullif(current_setting('app.allowed_clients', true), ''), ',')::uuid[]))"
)
_DATA_POLICY = f"firm_id = {_FIRM_CAST} AND {_CLIENT_MATCH}"

CREATE_TABLE = """
CREATE TABLE approval_matrix_rule (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id            uuid NOT NULL REFERENCES firm(id),
  client_id          uuid NOT NULL REFERENCES client(id),

  scope_department   text,                       -- NULL = applies to all departments
  scope_category_id  uuid REFERENCES category(id),-- NULL = applies to all categories
  min_amount         numeric(14,2),              -- band floor; NULL = 0
  max_amount         numeric(14,2),              -- band ceiling; NULL = unlimited
  step_order         integer NOT NULL DEFAULT 1, -- 1 = first approval; 2,3 = layers (Phase-2)
  approver_role      text,                       -- required role, else a specific person
  approver_user_id   uuid REFERENCES app_user(id),
  approvals_required integer NOT NULL DEFAULT 1, -- e.g. any 2 partners (multi-approval: Phase-2)
  active             boolean NOT NULL DEFAULT true,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_amr_role CHECK (
    approver_role IS NULL OR approver_role IN ('partner','manager','approver')),
  CONSTRAINT ck_amr_step CHECK (step_order >= 1),
  CONSTRAINT ck_amr_approvals CHECK (approvals_required >= 1),
  CONSTRAINT ck_amr_band CHECK (
    min_amount IS NULL OR max_amount IS NULL OR max_amount >= min_amount)
);
CREATE INDEX ix_amr_firm_client ON approval_matrix_rule(firm_id, client_id);
CREATE INDEX ix_amr_client_active ON approval_matrix_rule(client_id, active, step_order);

GRANT SELECT, INSERT, UPDATE, DELETE ON approval_matrix_rule TO onecapture_app;
"""

RLS = f"""
ALTER TABLE approval_matrix_rule ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_matrix_rule FORCE ROW LEVEL SECURITY;
CREATE POLICY approval_matrix_rule_tenant ON approval_matrix_rule FOR ALL
  USING ({_DATA_POLICY}) WITH CHECK ({_DATA_POLICY});
"""

DOWNGRADE = """
DROP POLICY IF EXISTS approval_matrix_rule_tenant ON approval_matrix_rule;
REVOKE SELECT, INSERT, UPDATE, DELETE ON approval_matrix_rule FROM onecapture_app;
DROP TABLE IF EXISTS approval_matrix_rule;
"""


def upgrade() -> None:
    op.execute(CREATE_TABLE)
    op.execute(RLS)


def downgrade() -> None:
    op.execute(DOWNGRADE)
