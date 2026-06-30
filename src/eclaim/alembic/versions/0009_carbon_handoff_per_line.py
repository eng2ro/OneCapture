"""e-Claim carbon handoff per line — stop computing tCO2e (Phase 1 cutover)

Revision ID: 0009_carbon_handoff_per_line
Revises: 0008_event_and_claim_line
Create Date: 2026-06-28

e-Claim stops computing emissions: it classifies each line (carbon_class + scope +
factor + basis) and FORWARDS the activity data; Carbon Next computes the tonnage.
So the shared ledger must accept a NULL ``tco2e``:

  * ``emission_entry.tco2e``     NOT NULL -> NULL  (e-Claim writes NULL; activity
                                                    data carries the meaning)
  * ``release_batch.total_tco2e`` NOT NULL -> NULL
  * ``emission_entry.carbon_class`` (new, nullable) — the class of the source line.

CRITICAL: ``emission_entry.tco2e`` / ``release_batch.total_tco2e`` are SHARED with
ERP Sync (src/erpsync/release/service.py), which still computes and writes real
tonnage. We only RELAX the NOT NULL — we never drop the column — so ERP Sync is
unaffected and keeps populating it.

The legacy per-receipt columns on ``claim`` (vendor … tco2e, image_path, …) are
deliberately KEPT for now: the service reads carbon from ``claim_line`` after this
cutover, leaving those columns vestigial. Dropping them is a later cleanup once the
new model has soaked — keeping them here means this migration carries no data loss.
"""

from alembic import op

revision = "0009_carbon_handoff_per_line"
down_revision = "0008_event_and_claim_line"
branch_labels = None
depends_on = None


UPGRADE = """
ALTER TABLE emission_entry ALTER COLUMN tco2e DROP NOT NULL;
ALTER TABLE release_batch  ALTER COLUMN total_tco2e DROP NOT NULL;
ALTER TABLE emission_entry ADD COLUMN carbon_class text
  CHECK (carbon_class IN ('direct','spend','none'));

-- The image + per-receipt data live on claim_line now; a claim is a header with
-- no image of its own. Relax the legacy NOT NULLs so a header inserts cleanly.
-- (The columns themselves stay for one release as vestigial; dropped later.)
ALTER TABLE claim ALTER COLUMN image_path   DROP NOT NULL;
ALTER TABLE claim ALTER COLUMN image_sha256 DROP NOT NULL;
"""

# Downgrade re-imposes NOT NULL. Any e-Claim rows written with NULL tco2e after the
# cutover would block this — the correct guard (you cannot un-relax with NULLs
# present). ERP Sync rows always have a value, so a pure-ERP install downgrades fine.
DOWNGRADE = """
ALTER TABLE claim ALTER COLUMN image_sha256 SET NOT NULL;
ALTER TABLE claim ALTER COLUMN image_path   SET NOT NULL;
ALTER TABLE emission_entry DROP COLUMN IF EXISTS carbon_class;
ALTER TABLE release_batch  ALTER COLUMN total_tco2e SET NOT NULL;
ALTER TABLE emission_entry ALTER COLUMN tco2e SET NOT NULL;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
