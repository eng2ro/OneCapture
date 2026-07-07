"""Document intake — classification + routing record for every captured page (C1)

Revision ID: 0025_document_intake
Revises: 0024_clamp_legacy_approvals
Create Date: 2026-07-07

Before a captured page becomes an e-Claim line (or, later, an AP invoice), it is
classified (expense_receipt / vendor_invoice / delivery_order / unknown) and routed.
This table is the durable record of that decision so:

* a vendor bill can sit visibly in the "Vendor bills (coming soon)" holding queue —
  captured now, processed when the AP module ships — instead of being silently forced
  into e-Claim;
* every routing decision (auto or a reviewer's correction) is auditable and reversible
  (re-route re-runs the right builder);
* a delivery order can be linked to its matching vendor invoice (same vendor + PO/DO
  ref) via ``link_key``.

Tenant-scoped (firm+client) under the SAME hardened, worker-inclusive RLS policy as
``ingestion_job`` (0018): the async ingestion worker also creates intake rows while
building a claim, so the policy admits the trusted ``app.worker='on'`` context in
addition to the normal per-tenant match. Empty GUC → NULL → deny (never a cast error).
"""

from alembic import op

revision = "0025_document_intake"
down_revision = "0024_clamp_legacy_approvals"
branch_labels = None
depends_on = None

_FIRM_MATCH = "firm_id = nullif(current_setting('app.current_firm', true), '')::uuid"
_CLIENT_MATCH = (
    "client_id = ANY(string_to_array("
    "nullif(current_setting('app.allowed_clients', true), ''), ',')::uuid[])"
)
_WORKER = "current_setting('app.worker', true) = 'on'"
_POLICY = f"(({_FIRM_MATCH} AND {_CLIENT_MATCH}) OR {_WORKER})"

UPGRADE = f"""
CREATE TABLE document_intake (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id             uuid NOT NULL REFERENCES firm(id),
    client_id           uuid NOT NULL REFERENCES client(id),
    created_by_user_id  uuid REFERENCES app_user(id),

    -- Image provenance (reuses the claim_line content-hash pattern).
    image_sha256        text,
    image_path          text,
    media_type          text,
    source_name         text,

    -- Classifier output (C1).
    document_type       text NOT NULL DEFAULT 'unknown',
    type_confidence     numeric(4,3),
    type_signals        jsonb NOT NULL DEFAULT '[]'::jsonb,

    -- Routing decision.
    routed_to           text NOT NULL DEFAULT 'pending',   -- eclaim | ap_holding | pending
    routed_by           text NOT NULL DEFAULT 'system',    -- system | user
    needs_manual        boolean NOT NULL DEFAULT false,
    status              text NOT NULL DEFAULT 'open',       -- open | consumed

    -- DO<->invoice linking (same vendor + PO/DO ref).
    link_key            text,
    linked_intake_id    uuid REFERENCES document_intake(id),

    -- Where an e-Claim-routed page landed (nullable until built).
    claim_id            uuid REFERENCES claim(id),

    -- Light denormalized fields for the holding-queue listing.
    vendor              text,
    doc_no              text,
    total_amount        numeric(14,2),
    currency            text,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_document_intake_type CHECK (
        document_type IN ('expense_receipt','vendor_invoice','delivery_order','unknown')),
    CONSTRAINT ck_document_intake_routed_to CHECK (
        routed_to IN ('eclaim','ap_holding','pending')),
    CONSTRAINT ck_document_intake_routed_by CHECK (routed_by IN ('system','user')),
    CONSTRAINT ck_document_intake_status CHECK (status IN ('open','consumed'))
);
CREATE INDEX ix_document_intake_firm_client ON document_intake(firm_id, client_id);
CREATE INDEX ix_document_intake_queue ON document_intake(client_id, routed_to, status, created_at);
CREATE INDEX ix_document_intake_link ON document_intake(client_id, link_key);

GRANT SELECT, INSERT, UPDATE, DELETE ON document_intake TO onecapture_app;

ALTER TABLE document_intake ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_intake FORCE ROW LEVEL SECURITY;
CREATE POLICY document_intake_tenant ON document_intake FOR ALL
    USING ({_POLICY}) WITH CHECK ({_POLICY});
"""

DOWNGRADE = """
DROP POLICY IF EXISTS document_intake_tenant ON document_intake;
ALTER TABLE document_intake NO FORCE ROW LEVEL SECURITY;
ALTER TABLE document_intake DISABLE ROW LEVEL SECURITY;
REVOKE SELECT, INSERT, UPDATE, DELETE ON document_intake FROM onecapture_app;
DROP TABLE IF EXISTS document_intake;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
