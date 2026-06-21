"""ERP Sync review lifecycle (FR-S5): approved/dismissed statuses + SoD actors

Extends ``erpsync_entry`` so a reviewer can work the held/flagged queue:

* widen ``status`` to add ``approved`` (human-reviewed, releasable alongside the
  auto ``clean`` rows) and ``dismissed`` (terminal — reject-as-duplicate and
  dismiss both land here, distinguished by their audit event, not the status);
* add SoD actor columns mirroring ``claim``: ``edited_by_user_id`` (the maker who
  last remapped/edited a flagged row) and ``reviewed_by_user_id`` (the checker who
  approved/dismissed it), plus ``reviewed_at`` and a free-text ``review_note``;
* add the static SoD second layer ``ck_erpsync_entry_sod`` — the user who edited a
  row cannot be the one who reviews it (maker-checker), byte-for-byte the idea
  behind ``ck_claim_sod``. The dynamic guard runs at the service layer under the
  live principal; this CHECK is defence in depth.

No RLS / grant changes — the new columns inherit ``erpsync_entry``'s existing
firm/allowed-client policy, and ``onecapture_app`` already holds DML on the table.
Runs as the owner (bypasses RLS), so the ALTERs apply before the policy bites.
"""

from alembic import op

revision = "0005_erpsync_review_lifecycle"
down_revision = "0004_erpsync_entry_staging"
branch_labels = None
depends_on = None


UPGRADE = """
-- 1. Widen the review status vocabulary (clean/held/flagged/released
--    -> + approved + dismissed). 0004 created the status CHECK inline, so
--    Postgres auto-named it 'erpsync_entry_status_check'; we drop that and
--    re-add it under the model's explicit name, aligning the two going forward.
ALTER TABLE erpsync_entry DROP CONSTRAINT erpsync_entry_status_check;
ALTER TABLE erpsync_entry ADD CONSTRAINT ck_erpsync_entry_status
  CHECK (status IN ('clean','held','flagged','approved','dismissed','released'));

-- 2. SoD actor + review-provenance columns (nullable: auto-clean rows carry none).
ALTER TABLE erpsync_entry
  ADD COLUMN edited_by_user_id   uuid REFERENCES app_user(id),
  ADD COLUMN reviewed_by_user_id uuid REFERENCES app_user(id),
  ADD COLUMN reviewed_at         timestamptz,
  ADD COLUMN review_note         text;

-- 3. SoD second layer: the maker (editor) cannot be the checker (reviewer).
ALTER TABLE erpsync_entry ADD CONSTRAINT ck_erpsync_entry_sod
  CHECK (reviewed_by_user_id IS NULL OR reviewed_by_user_id <> edited_by_user_id);
"""

DOWNGRADE = """
ALTER TABLE erpsync_entry DROP CONSTRAINT IF EXISTS ck_erpsync_entry_sod;
ALTER TABLE erpsync_entry
  DROP COLUMN IF EXISTS review_note,
  DROP COLUMN IF EXISTS reviewed_at,
  DROP COLUMN IF EXISTS reviewed_by_user_id,
  DROP COLUMN IF EXISTS edited_by_user_id;
ALTER TABLE erpsync_entry DROP CONSTRAINT IF EXISTS ck_erpsync_entry_status;
ALTER TABLE erpsync_entry ADD CONSTRAINT erpsync_entry_status_check
  CHECK (status IN ('clean','held','flagged','released'));
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
