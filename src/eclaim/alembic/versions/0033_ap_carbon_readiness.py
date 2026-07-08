"""AP carbon readiness: intake keeps the full OCR read, AP lines snapshot carbon
relevance, and the handoff can carry AP parents (F-E items 9, 10, 13)

Revision ID: 0033_ap_carbon_readiness
Revises: 0032_handoff_payload_fields
Create Date: 2026-07-07

Three schema gaps blocked the AP carbon handoff:

1. ``document_intake`` persisted only vendor/doc_no/total/currency — the OCR's
   doc_date, tax, quantity, unit and expense_type were read then DISCARDED at the
   divert step, so every AP line would have forwarded quantity=NULL and dateless.
2. ``ap_invoice_line`` had no ``carbon_relevant`` snapshot (claim_line snapshots it
   at classify time precisely so later category edits don't rewrite history).
3. ``carbon_handoff.claim_id``/``line_id`` were NOT NULL — an AP row could not be
   inserted at all. They become nullable with AP parent columns and a CHECK that
   exactly one parent pair is set.
"""

from alembic import op

revision = "0033_ap_carbon_readiness"
down_revision = "0032_handoff_payload_fields"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE document_intake
  ADD COLUMN doc_date     text,
  ADD COLUMN tax_amount   numeric(14,2),
  ADD COLUMN tax_code     text,
  ADD COLUMN quantity     numeric(14,4),
  ADD COLUMN unit         text,
  ADD COLUMN expense_type text;

ALTER TABLE ap_invoice_line
  ADD COLUMN carbon_relevant boolean;

ALTER TABLE carbon_handoff
  ALTER COLUMN claim_id DROP NOT NULL,
  ALTER COLUMN line_id  DROP NOT NULL,
  ADD COLUMN ap_invoice_id uuid REFERENCES ap_invoice(id),
  ADD COLUMN ap_line_id    uuid REFERENCES ap_invoice_line(id),
  ADD CONSTRAINT ck_carbon_handoff_parent CHECK (
    (claim_id IS NOT NULL AND line_id IS NOT NULL
       AND ap_invoice_id IS NULL AND ap_line_id IS NULL)
    OR
    (ap_invoice_id IS NOT NULL AND ap_line_id IS NOT NULL
       AND claim_id IS NULL AND line_id IS NULL)
  );
CREATE INDEX ix_carbon_handoff_ap_line ON carbon_handoff(ap_line_id);
"""

DOWNGRADE = """
DROP INDEX IF EXISTS ix_carbon_handoff_ap_line;
DELETE FROM carbon_handoff WHERE ap_invoice_id IS NOT NULL;
ALTER TABLE carbon_handoff
  DROP CONSTRAINT ck_carbon_handoff_parent,
  DROP COLUMN ap_line_id,
  DROP COLUMN ap_invoice_id;
ALTER TABLE carbon_handoff
  ALTER COLUMN claim_id SET NOT NULL,
  ALTER COLUMN line_id  SET NOT NULL;

ALTER TABLE ap_invoice_line DROP COLUMN carbon_relevant;

ALTER TABLE document_intake
  DROP COLUMN expense_type,
  DROP COLUMN unit,
  DROP COLUMN quantity,
  DROP COLUMN tax_code,
  DROP COLUMN tax_amount,
  DROP COLUMN doc_date;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
