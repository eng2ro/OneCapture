"""Accounts-payable domain: vendor + ap_invoice + ap_invoice_line, matrix scope (C2)

Revision ID: 0026_ap_domain
Revises: 0025_document_intake
Create Date: 2026-07-07

The AP module (workflow first, ERP stubbed): a vendor bill the classifier diverted
(C1) becomes a coded, approved, exportable AP invoice — a bill FINANCE pays, never a
staff reimbursement. Three tenant-scoped tables, all under the SAME hardened RLS
policy as the other e-Claim data tables (firm + allowed-client, empty GUC → NULL →
deny):

* ``vendor``      — the supplier master (name, tax id, bank ref, ERP code once mapped).
* ``ap_invoice``  — the header: doc no/date, due date, terms, money (Decimal), PO/DO
  refs, image provenance, lifecycle status, the ERP key after posting, and an
  idempotency key. Separation of duties is a DB CHECK: whoever CODED the invoice
  cannot be the one who APPROVED it (``coded_by_user_id <> approved_by_user_id``).
* ``ap_invoice_line`` — description, qty/uom/unit price/line total, GL + tax codes,
  a carbon category (raw activity data → CarbonNext, same pattern as e-Claim), and
  cost dims (department / project).

Also adds ``scope_module`` to ``approval_matrix_rule`` NOW (nullable, NULL = every
module) so the shared Appendix-B engine can carry different bands for ``eclaim`` vs
``ap`` with no later migration — existing rows stay NULL and keep applying to e-Claim.
"""

from alembic import op

revision = "0026_ap_domain"
down_revision = "0025_document_intake"
branch_labels = None
depends_on = None

_FIRM_CAST = "nullif(current_setting('app.current_firm', true), '')::uuid"
_CLIENT_MATCH = (
    "(client_id IS NULL OR client_id = ANY("
    "string_to_array(nullif(current_setting('app.allowed_clients', true), ''), ',')::uuid[]))"
)
_DATA_POLICY = f"firm_id = {_FIRM_CAST} AND {_CLIENT_MATCH}"


def _rls(table: str) -> str:
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY {table}_tenant ON {table} FOR ALL
  USING ({_DATA_POLICY}) WITH CHECK ({_DATA_POLICY});
GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO onecapture_app;
"""


VENDOR = """
CREATE TABLE vendor (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id           uuid NOT NULL REFERENCES firm(id),
  client_id         uuid NOT NULL REFERENCES client(id),
  name              text NOT NULL,
  tax_id            text,                         -- SST / e-Invoice TIN
  bank_account      text,                         -- payment reference only (NOT card data)
  erp_vendor_code   text,                         -- NULL until mapped to the ERP
  status            text NOT NULL DEFAULT 'active',
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_vendor_status CHECK (status IN ('active','inactive'))
);
CREATE INDEX ix_vendor_firm_client ON vendor(firm_id, client_id);
CREATE INDEX ix_vendor_name ON vendor(client_id, lower(name));
-- One ERP code per client (nullable codes don't collide — partial unique).
CREATE UNIQUE INDEX uq_vendor_erp_code ON vendor(client_id, erp_vendor_code)
  WHERE erp_vendor_code IS NOT NULL;
"""

AP_INVOICE = """
CREATE TABLE ap_invoice (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id            uuid NOT NULL REFERENCES firm(id),
  client_id          uuid NOT NULL REFERENCES client(id),
  vendor_id          uuid NOT NULL REFERENCES vendor(id),

  doc_no             text,
  doc_date           date,
  due_date           date,
  payment_terms      text,
  currency           text NOT NULL DEFAULT 'MYR',
  subtotal           numeric(14,2),
  tax_amount         numeric(14,2),
  total_amount       numeric(14,2),
  po_ref             text,
  do_ref             text,

  image_sha256       text,
  image_path         text,
  intake_id          uuid REFERENCES document_intake(id),

  status             text NOT NULL DEFAULT 'captured',
  erp_doc_entry      text,                        -- the ERP's key once posted
  idempotency_key    text NOT NULL,

  coded_by_user_id   uuid REFERENCES app_user(id),
  approved_by_user_id uuid REFERENCES app_user(id),
  approved_at        timestamptz,
  hold_reason        text,

  created_by_user_id uuid REFERENCES app_user(id),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_ap_invoice_status CHECK (status IN (
    'captured','coded','pending_approval','approved','posted','paid','held','rejected')),
  -- Separation of duties: the coder/keyer cannot also be the approver.
  CONSTRAINT ck_ap_invoice_sod CHECK (
    coded_by_user_id IS NULL OR approved_by_user_id IS NULL
    OR coded_by_user_id <> approved_by_user_id)
);
CREATE INDEX ix_ap_invoice_firm_client ON ap_invoice(firm_id, client_id);
CREATE INDEX ix_ap_invoice_status ON ap_invoice(client_id, status, created_at);
-- Duplicate-payment guard: same vendor + doc_no is the classic double-pay — indexed
-- so the service can flag it hard (a UNIQUE would block legitimate re-captures, so
-- detection is a service decision, not a blanket constraint).
CREATE INDEX ix_ap_invoice_dup ON ap_invoice(client_id, vendor_id, doc_no);
-- The idempotency key blocks an accidental double-insert of the same source doc.
CREATE UNIQUE INDEX uq_ap_invoice_idem ON ap_invoice(client_id, idempotency_key);
"""

AP_INVOICE_LINE = """
CREATE TABLE ap_invoice_line (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id           uuid NOT NULL REFERENCES firm(id),
  client_id         uuid NOT NULL REFERENCES client(id),
  ap_invoice_id     uuid NOT NULL REFERENCES ap_invoice(id) ON DELETE CASCADE,
  line_no           integer NOT NULL DEFAULT 1,

  description       text,
  quantity          numeric(14,4),
  uom               text,
  unit_price        numeric(14,4),
  line_total        numeric(14,2),

  gl_code           text,
  tax_code          text,
  category_id       uuid REFERENCES category(id),   -- carbon relevance (→ CarbonNext)
  department        text,
  project_code      text,

  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_ap_invoice_line_no UNIQUE (ap_invoice_id, line_no)
);
CREATE INDEX ix_ap_invoice_line_invoice ON ap_invoice_line(ap_invoice_id);
CREATE INDEX ix_ap_invoice_line_firm_client ON ap_invoice_line(firm_id, client_id);
"""

SCOPE_MODULE = """
ALTER TABLE approval_matrix_rule ADD COLUMN scope_module text;
ALTER TABLE approval_matrix_rule ADD CONSTRAINT ck_amr_scope_module
  CHECK (scope_module IS NULL OR scope_module IN ('eclaim','ap'));
"""

DOWNGRADE = """
ALTER TABLE approval_matrix_rule DROP CONSTRAINT IF EXISTS ck_amr_scope_module;
ALTER TABLE approval_matrix_rule DROP COLUMN IF EXISTS scope_module;
DROP POLICY IF EXISTS ap_invoice_line_tenant ON ap_invoice_line;
DROP TABLE IF EXISTS ap_invoice_line;
DROP POLICY IF EXISTS ap_invoice_tenant ON ap_invoice;
DROP TABLE IF EXISTS ap_invoice;
DROP POLICY IF EXISTS vendor_tenant ON vendor;
DROP TABLE IF EXISTS vendor;
"""


def upgrade() -> None:
    op.execute(VENDOR)
    op.execute(_rls("vendor"))
    op.execute(AP_INVOICE)
    op.execute(_rls("ap_invoice"))
    op.execute(AP_INVOICE_LINE)
    op.execute(_rls("ap_invoice_line"))
    op.execute(SCOPE_MODULE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
