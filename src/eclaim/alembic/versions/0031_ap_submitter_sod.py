"""Track the AP submitter + widen the SoD CHECK to filer/submitter ≠ approver (F5)

Revision ID: 0031_ap_submitter_sod
Revises: 0030_intake_doc_types
Create Date: 2026-07-07

F5 closed the uncoded-submit/self-approve exploit at the service layer, but left two
gaps the audit called out: the SUBMITTER of a coded invoice was never recorded (so a
third user could submit and then approve with no trace of the overlap), and the DB
CHECK only covered coder ≠ approver — weaker than e-Claim's ``ck_claim_sod``, which
backs the service rule at the database.

This migration adds ``submitted_by_user_id`` and widens ``ck_ap_invoice_sod`` so that
NONE of the three preparer roles (filer, coder, submitter) can be the approver, each
comparison null-safe — mirroring the service-layer checks exactly. Existing rows have
a NULL submitter (pre-F5 submissions weren't attributed), which the CHECK permits.
"""

from alembic import op

revision = "0031_ap_submitter_sod"
down_revision = "0030_intake_doc_types"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE ap_invoice
  ADD COLUMN submitted_by_user_id uuid REFERENCES app_user(id);

ALTER TABLE ap_invoice DROP CONSTRAINT ck_ap_invoice_sod;
ALTER TABLE ap_invoice ADD CONSTRAINT ck_ap_invoice_sod CHECK (
  (coded_by_user_id     IS NULL OR approved_by_user_id IS NULL
     OR coded_by_user_id     <> approved_by_user_id)
  AND
  (created_by_user_id   IS NULL OR approved_by_user_id IS NULL
     OR created_by_user_id   <> approved_by_user_id)
  AND
  (submitted_by_user_id IS NULL OR approved_by_user_id IS NULL
     OR submitted_by_user_id <> approved_by_user_id)
);
"""

DOWNGRADE = """
ALTER TABLE ap_invoice DROP CONSTRAINT ck_ap_invoice_sod;
ALTER TABLE ap_invoice ADD CONSTRAINT ck_ap_invoice_sod CHECK (
  coded_by_user_id IS NULL OR approved_by_user_id IS NULL
  OR coded_by_user_id <> approved_by_user_id
);
ALTER TABLE ap_invoice DROP COLUMN submitted_by_user_id;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
