"""e-Claim corporate expense redesign — event + claim_line + claim header (Phase 0)

Revision ID: 0008_event_and_claim_line
Revises: 0007_relax_category_unique
Create Date: 2026-06-27

Additive, ship-safe split of the single-receipt ``claim`` into a multi-line model:

  * NEW ``event``      — optional trip/training grouping holding purpose,
                         attendee_count, dates, location, cost_centre, project and
                         a BUDGET. Aggregates across many claims and many people.
  * NEW ``claim_line`` — the per-receipt record (most of today's ``claim``): OCR
                         fields + carbon classification, plus reimbursement fields
                         (tax, payment_method, GL) and a per-line review status for
                         partial approval. Carries ``carbon_class`` (direct/spend/
                         none); ``tco2e`` is deliberately NOT carried — Carbon Next
                         computes emissions from the forwarded activity data.
  * ``claim`` gains header columns (event_id, title, purpose, totals, approver_note)
    and a widened status CHECK (partially_approved / sent_back / exported / paid).
  * ``category`` gains ``carbon_class`` — the curated carbon class per category.

PURELY ADDITIVE: every legacy ``claim`` column stays, and the migration backfills
one ``claim_line`` per existing ``claim``, so the app keeps running on the legacy
columns until the Phase-1 cutover (0009) drops them.

Tenant-scoped + RLS with the SAME hardened policy as the other e-Claim data tables
(the 0003/0006 ``nullif(...,'')::uuid`` firm cast), so event/claim_line isolate
exactly like claim/claimant/etc. Runs as the admin/owner: CREATE + GRANT + policy +
backfill all happen before RLS bites for onecapture_app, and the backfill (owner
bypasses RLS) reaches every existing row.
"""

from alembic import op

revision = "0008_event_and_claim_line"
down_revision = "0007_relax_category_unique"
branch_labels = None
depends_on = None

# Byte-identical to the post-0003 data-table policy (the hardened firm cast from
# 0006), so event/claim_line isolate exactly like claim/claimant/etc.
_FIRM_CAST = "nullif(current_setting('app.current_firm', true), '')::uuid"
_CLIENT_MATCH = (
    "(client_id IS NULL OR client_id = ANY("
    "string_to_array(nullif(current_setting('app.allowed_clients', true), ''), ',')::uuid[]))"
)
_DATA_POLICY = f"firm_id = {_FIRM_CAST} AND {_CLIENT_MATCH}"


CREATE_EVENT = """
CREATE TABLE event (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id           uuid NOT NULL REFERENCES firm(id),
  client_id         uuid NOT NULL REFERENCES client(id),

  title             text NOT NULL,
  purpose           text,
  event_type        text,                  -- training / travel / client_meeting / ...
  attendee_count    int,
  start_date        date,
  end_date          date,
  location          text,
  department        text,
  cost_centre       text,
  project_code      text,
  budget_amount     numeric(14,2),
  budget_currency   text,
  organiser_user_id uuid REFERENCES app_user(id),
  status            text NOT NULL DEFAULT 'active',
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_event_firm_client ON event(firm_id, client_id);
GRANT SELECT, INSERT, UPDATE, DELETE ON event TO onecapture_app;
"""

# claim_line carries denormalised firm_id + client_id so the SAME _DATA_POLICY RLS
# applies as on claim. image_path/image_sha256 stay NOT NULL (they are on claim).
CREATE_CLAIM_LINE = """
CREATE TABLE claim_line (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id              uuid NOT NULL REFERENCES firm(id),
  client_id            uuid NOT NULL REFERENCES client(id),
  claim_id             uuid NOT NULL REFERENCES claim(id),
  line_no              int  NOT NULL DEFAULT 1,

  -- OCR-extracted (moved from claim) --------------------------------------
  vendor               text,
  doc_no               text,
  doc_date             text,
  currency             text,
  total_amount         numeric(14,2),
  expense_type         text,
  quantity             numeric(14,4),
  unit                 text,
  ocr_confidence       numeric(4,3),
  image_path           text NOT NULL,
  image_sha256         text NOT NULL,

  -- Reimbursement fields --------------------------------------------------
  business_reason      text,
  tax_amount           numeric(14,2),
  tax_code             text,
  tax_inclusive        boolean,
  net_amount           numeric(14,2),
  fx_rate              numeric(18,6),
  base_amount          numeric(14,2),
  payment_method       text NOT NULL DEFAULT 'out_of_pocket'
                       CHECK (payment_method IN ('out_of_pocket','corporate_card','company_paid')),
  reimbursable         boolean NOT NULL DEFAULT true,
  gl_code              text,
  cost_centre_override text,
  attendees            jsonb,
  mileage              jsonb,
  per_diem             jsonb,
  policy_result        text,

  -- Carbon classification (moved from claim; tco2e intentionally NOT carried) -
  scope                smallint CHECK (scope IN (1,2,3)),
  factor_key           text,
  factor_version       int,
  basis                text CHECK (basis IN ('activity','spend')),
  data_quality         text,
  category_id          uuid REFERENCES category(id),
  carbon_class         text NOT NULL DEFAULT 'none'
                       CHECK (carbon_class IN ('direct','spend','none')),

  -- Per-line review state (partial approval) ------------------------------
  line_status          text NOT NULL DEFAULT 'pending'
                       CHECK (line_status IN ('pending','approved','queried','rejected')),
  line_reason          text,

  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_claim_line_claim ON claim_line(claim_id);
CREATE INDEX ix_claim_line_firm_client ON claim_line(firm_id, client_id);
GRANT SELECT, INSERT, UPDATE, DELETE ON claim_line TO onecapture_app;
"""

# Header columns on claim (additive). event_id references the new event table.
CLAIM_HEADER = """
ALTER TABLE claim
  ADD COLUMN event_id           uuid REFERENCES event(id),
  ADD COLUMN title              text,
  ADD COLUMN purpose            text,
  ADD COLUMN claim_currency     text,
  ADD COLUMN period             text,
  ADD COLUMN total_claimed      numeric(14,2),
  ADD COLUMN total_approved     numeric(14,2),
  ADD COLUMN total_reimbursable numeric(14,2),
  ADD COLUMN approver_note      text;
CREATE INDEX ix_claim_event ON claim(event_id);
"""

# Widen the status CHECK. 0001 created it inline as `claim_status_check`; replace
# it with a named `ck_claim_status` carrying the partial-approval + settlement
# states.
WIDEN_STATUS = """
ALTER TABLE claim DROP CONSTRAINT IF EXISTS claim_status_check;
ALTER TABLE claim ADD CONSTRAINT ck_claim_status CHECK (
  status IN ('submitted','in_review','approved','partially_approved',
             'sent_back','rejected','released','exported','paid')
);
"""

# category.carbon_class — curated per category. Backfill preserves today's
# behaviour: a real factor => 'direct'; spend-less (NULL factor_key) => 'spend'
# (governed spend, NOT excluded). Admins curate genuine 'none' later.
CATEGORY_CARBON_CLASS = """
ALTER TABLE category ADD COLUMN carbon_class text NOT NULL DEFAULT 'none'
  CHECK (carbon_class IN ('direct','spend','none'));
UPDATE category
   SET carbon_class = CASE WHEN factor_key IS NOT NULL THEN 'direct' ELSE 'spend' END;
"""

# Backfill one claim_line per existing claim (owner connection bypasses RLS, so it
# reaches every row). carbon_class comes from the joined category when set, else
# defaults to 'spend' (never silently 'none'). line_status mirrors the claim's
# lifecycle so already-approved claims have an approved line.
BACKFILL_LINES = """
INSERT INTO claim_line (
  firm_id, client_id, claim_id, line_no,
  vendor, doc_no, doc_date, currency, total_amount, expense_type, quantity, unit,
  ocr_confidence, image_path, image_sha256, net_amount,
  scope, factor_key, factor_version, basis, data_quality, category_id, carbon_class,
  payment_method, reimbursable, line_status
)
SELECT
  c.firm_id, c.client_id, c.id, 1,
  c.vendor, c.doc_no, c.doc_date, c.currency, c.total_amount, c.expense_type, c.quantity, c.unit,
  c.ocr_confidence, c.image_path, c.image_sha256, c.total_amount,
  c.scope, c.factor_key, c.factor_version, c.basis, c.data_quality, c.category_id,
  COALESCE(cat.carbon_class, 'spend'),
  'out_of_pocket', true,
  CASE
    WHEN c.status IN ('approved','released','exported','paid') THEN 'approved'
    WHEN c.status = 'rejected'                                 THEN 'rejected'
    ELSE 'pending'
  END
FROM claim c
LEFT JOIN category cat ON cat.id = c.category_id;
"""

# Header totals from the single backfilled line.
BACKFILL_TOTALS = """
UPDATE claim SET
  total_claimed      = total_amount,
  total_approved     = CASE WHEN status IN ('approved','released','exported','paid')
                            THEN total_amount ELSE NULL END,
  total_reimbursable = CASE WHEN status IN ('approved','released','exported','paid')
                            THEN total_amount ELSE NULL END;
"""

RLS = f"""
ALTER TABLE event ENABLE ROW LEVEL SECURITY;
ALTER TABLE event FORCE ROW LEVEL SECURITY;
CREATE POLICY event_tenant ON event FOR ALL
  USING ({_DATA_POLICY}) WITH CHECK ({_DATA_POLICY});

ALTER TABLE claim_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE claim_line FORCE ROW LEVEL SECURITY;
CREATE POLICY claim_line_tenant ON claim_line FOR ALL
  USING ({_DATA_POLICY}) WITH CHECK ({_DATA_POLICY});
"""

DOWNGRADE = """
DROP POLICY IF EXISTS claim_line_tenant ON claim_line;
DROP POLICY IF EXISTS event_tenant ON event;

ALTER TABLE category DROP COLUMN IF EXISTS carbon_class;

ALTER TABLE claim DROP CONSTRAINT IF EXISTS ck_claim_status;
ALTER TABLE claim ADD CONSTRAINT claim_status_check
  CHECK (status IN ('submitted','in_review','approved','released','rejected'));

DROP INDEX IF EXISTS ix_claim_event;
ALTER TABLE claim
  DROP COLUMN IF EXISTS approver_note,
  DROP COLUMN IF EXISTS total_reimbursable,
  DROP COLUMN IF EXISTS total_approved,
  DROP COLUMN IF EXISTS total_claimed,
  DROP COLUMN IF EXISTS period,
  DROP COLUMN IF EXISTS claim_currency,
  DROP COLUMN IF EXISTS purpose,
  DROP COLUMN IF EXISTS title,
  DROP COLUMN IF EXISTS event_id;

REVOKE SELECT, INSERT, UPDATE, DELETE ON claim_line FROM onecapture_app;
DROP TABLE IF EXISTS claim_line;
REVOKE SELECT, INSERT, UPDATE, DELETE ON event FROM onecapture_app;
DROP TABLE IF EXISTS event;
"""


def upgrade() -> None:
    op.execute(CREATE_EVENT)
    op.execute(CREATE_CLAIM_LINE)
    op.execute(CLAIM_HEADER)
    op.execute(WIDEN_STATUS)
    op.execute(CATEGORY_CARBON_CLASS)
    op.execute(BACKFILL_LINES)
    op.execute(BACKFILL_TOTALS)
    op.execute(RLS)


def downgrade() -> None:
    op.execute(DOWNGRADE)
