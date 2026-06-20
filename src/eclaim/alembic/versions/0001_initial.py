"""initial OneCapture schema (shared tables + e-Claim)

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-16

Creates the full schema from eclaim_postgres_spec.md §3 as raw DDL so it matches
the spec byte-for-byte (CHECK constraints, ``gen_random_uuid()`` defaults, the
``pgcrypto`` extension). The SQLAlchemy models in ``eclaim.db.models`` mirror this.
"""

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


DDL = r"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE client (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,
  ssm_no      text UNIQUE,
  currency    text NOT NULL DEFAULT 'MYR',
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE emission_factor (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  factor_key          text NOT NULL,
  label               text NOT NULL,
  scope               smallint NOT NULL CHECK (scope IN (1,2,3)),
  unit                text NOT NULL,
  factor_kg_per_unit  numeric(12,5) NOT NULL,
  source              text,
  version             int NOT NULL DEFAULT 1,
  effective_from      date NOT NULL DEFAULT current_date,
  active              boolean NOT NULL DEFAULT true,
  UNIQUE (factor_key, version)
);

CREATE TABLE claim (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id       uuid NOT NULL REFERENCES client(id),
  source_channel  text NOT NULL DEFAULT 'upload',
  claimant_ref    text,
  received_at     timestamptz NOT NULL DEFAULT now(),
  vendor          text,
  doc_no          text,
  doc_date        text,
  currency        text,
  total_amount    numeric(14,2),
  expense_type    text,
  quantity        numeric(14,4),
  unit            text,
  ocr_confidence  numeric(4,3),
  image_path      text NOT NULL,
  image_sha256    text NOT NULL,
  scope           smallint CHECK (scope IN (1,2,3)),
  factor_key      text,
  factor_version  int,
  basis           text CHECK (basis IN ('activity','spend')),
  tco2e           numeric(16,6),
  data_quality    text,
  status          text NOT NULL DEFAULT 'in_review'
                  CHECK (status IN ('submitted','in_review','approved','released','rejected')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_claim_client_status ON claim(client_id, status);

CREATE TABLE release_batch (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id     uuid NOT NULL REFERENCES client(id),
  source_type   text NOT NULL CHECK (source_type IN ('eclaim','erpsync')),
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  batch_hash    text NOT NULL,
  tsa_token     text,
  record_count  int NOT NULL,
  total_tco2e   numeric(16,6) NOT NULL,
  status        text NOT NULL DEFAULT 'released'
);

CREATE TABLE emission_entry (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id         uuid NOT NULL REFERENCES client(id),
  source_type       text NOT NULL CHECK (source_type IN ('eclaim','erpsync')),
  source_id         uuid NOT NULL,
  scope             smallint NOT NULL CHECK (scope IN (1,2,3)),
  factor_key        text NOT NULL,
  factor_version    int  NOT NULL,
  quantity          numeric(14,4),
  unit              text,
  basis             text NOT NULL CHECK (basis IN ('activity','spend')),
  tco2e             numeric(16,6) NOT NULL,
  release_batch_id  uuid NOT NULL REFERENCES release_batch(id),
  idempotency_key   text NOT NULL UNIQUE,
  carbon_ref        text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_entry_client_batch ON emission_entry(client_id, release_batch_id);

CREATE TABLE audit_event (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id    uuid NOT NULL REFERENCES client(id),
  entity_type  text NOT NULL,
  entity_id    uuid NOT NULL,
  event_type   text NOT NULL,
  actor        text NOT NULL,
  detail       jsonb,
  prev_hash    text,
  hash         text NOT NULL,
  ip           text,
  device       text,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_entity ON audit_event(entity_type, entity_id);
"""

DROP = r"""
DROP TABLE IF EXISTS audit_event;
DROP TABLE IF EXISTS emission_entry;
DROP TABLE IF EXISTS release_batch;
DROP TABLE IF EXISTS claim;
DROP TABLE IF EXISTS emission_factor;
DROP TABLE IF EXISTS client;
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute(DROP)
