# OneCapture e-Claim — Real Build Spec (Postgres)

**Status:** engineering spec for the first real build · consistent with OneCapture Startup Requirements v1.2 (FR-E1–E9) and the existing ERP Sync engine.
**How to use:** drop this file in the project root (`C:\dev\onecapture`). Point Claude Code at it: *"Read eclaim_postgres_spec.md and build it. Propose the stack, schema, and migrations first, wait for my OK, then build."* Build against this exactly; deviations are decisions, not guesses.

This spec turns e-Claim from a demo into a real, database-backed module of the same OneCapture app where ERP Sync already lives. **One app, one Postgres database, two modules** (e-Claim + ERP Sync) sharing the carbon logic, the release gate, and the audit chain.

---

## 1. Scope of this build

**In scope (first real chunk):**
- A real PostgreSQL database behind the existing repository seam, with Alembic migrations.
- e-Claim module: receipt upload → live OCR (Claude vision) → carbon classification → human review/edit → approve → release → persisted emission ledger.
- A simple real web UI: upload page, review page, ledger page.
- Shared release gate (batch hash, idempotency, irreversibility) and hash-chained audit trail — reused from ERP Sync, not reinvented.

**Deferred — leave clean seams, do not build now:**
- Multi-tenant isolation + firm-level scoping + Entra ID / SSO auth (single firm for now).
- WhatsApp and email intake (only `upload` channel now).
- Real RFC 3161 TSA anchoring (stub the token for now).
- Carbon Next ingestion API (posting entries out to the carbon platform).
- Object storage for images / pre-signed URLs (local disk for now).
- Notification engine; multi-tier approval thresholds (single approve→release step now).

---

## 2. Architecture

- **Backend:** Python + FastAPI (match the ERP Sync stack already in the repo).
- **DB:** PostgreSQL via SQLAlchemy 2.x; Alembic for migrations. Connection from `DATABASE_URL`.
- **Layers:** API routers → services (domain logic) → repositories (interfaces + SQLAlchemy impl) → Postgres. The repository interface is the seam already established by ERP Sync — implement the Postgres repository against it so both modules share it.
- **OCR:** an `OcrProvider` interface with an `AnthropicVisionProvider` implementation (model `claude-sonnet-4-6`), key from `ANTHROPIC_API_KEY`. The interface lets a dedicated OCR vendor swap in later (production accuracy/cost — decision D4).
- **Shared modules (factor out so both e-Claim and ERP Sync call them):**
  - `carbon` — emission factors + scope/quantity classification.
  - `release` — batch hashing, idempotency, irreversibility, TSA token (stub).
  - `audit` — hash-chained event writer.
- **Config:** pydantic-settings reading a `.env` (`DATABASE_URL`, `ANTHROPIC_API_KEY`). A separate test DB URL for tests.

---

## 3. Database schema (Postgres)

Shared tables serve both modules; `source_type` discriminates origin. UUID PKs, `timestamptz`, `numeric` for money/emissions. Requires `pgcrypto` (for `gen_random_uuid()`).

```sql
-- companies being measured (firm_id added when multi-tenant lands)
CREATE TABLE client (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,
  ssm_no      text UNIQUE,
  currency    text NOT NULL DEFAULT 'MYR',
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- versioned emission-factor library (SHARED with ERP Sync). Values are demo
-- placeholders until the carbon lead sets the real set (decision D14).
CREATE TABLE emission_factor (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  factor_key          text NOT NULL,                 -- 'fuel_diesel','fuel_petrol','electricity','natural_gas','air_travel'
  label               text NOT NULL,
  scope               smallint NOT NULL CHECK (scope IN (1,2,3)),
  unit                text NOT NULL,                 -- 'L','kWh','m3','km'
  factor_kg_per_unit  numeric(12,5) NOT NULL,
  source              text,                          -- 'MY grid 2026', 'DEFRA 2026', ...
  version             int NOT NULL DEFAULT 1,
  effective_from      date NOT NULL DEFAULT current_date,
  active              boolean NOT NULL DEFAULT true,
  UNIQUE (factor_key, version)
);

-- e-Claim documents
CREATE TABLE claim (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id       uuid NOT NULL REFERENCES client(id),
  source_channel  text NOT NULL DEFAULT 'upload',    -- 'upload' now; 'whatsapp'|'email' later
  claimant_ref    text,                              -- optional for now
  received_at     timestamptz NOT NULL DEFAULT now(),
  -- OCR-extracted fields
  vendor          text,
  doc_no          text,
  doc_date        text,
  currency        text,
  total_amount    numeric(14,2),
  expense_type    text,                              -- fuel_diesel|fuel_petrol|electricity|natural_gas|air_travel|other
  quantity        numeric(14,4),
  unit            text,
  ocr_confidence  numeric(4,3),
  -- source image (local disk now; object-storage key later)
  image_path      text NOT NULL,
  image_sha256    text NOT NULL,
  -- classification (computed by the carbon module)
  scope           smallint CHECK (scope IN (1,2,3)),
  factor_key      text,
  factor_version  int,
  basis           text CHECK (basis IN ('activity','spend')),
  tco2e           numeric(16,6),
  data_quality    text,
  -- lifecycle
  status          text NOT NULL DEFAULT 'in_review'
                  CHECK (status IN ('submitted','in_review','approved','released','rejected')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_claim_client_status ON claim(client_id, status);

-- release batches (SHARED)
CREATE TABLE release_batch (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id     uuid NOT NULL REFERENCES client(id),
  source_type   text NOT NULL CHECK (source_type IN ('eclaim','erpsync')),
  created_by    text NOT NULL,                        -- releaser identity
  created_at    timestamptz NOT NULL DEFAULT now(),
  batch_hash    text NOT NULL,
  tsa_token     text,                                 -- STUB now; real RFC 3161 later
  record_count  int NOT NULL,
  total_tco2e   numeric(16,6) NOT NULL,
  status        text NOT NULL DEFAULT 'released'
);

-- the carbon ledger (SHARED by e-Claim + ERP Sync)
CREATE TABLE emission_entry (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id         uuid NOT NULL REFERENCES client(id),
  source_type       text NOT NULL CHECK (source_type IN ('eclaim','erpsync')),
  source_id         uuid NOT NULL,                    -- claim.id or erp record id
  scope             smallint NOT NULL CHECK (scope IN (1,2,3)),
  factor_key        text NOT NULL,
  factor_version    int  NOT NULL,
  quantity          numeric(14,4),
  unit              text,
  basis             text NOT NULL CHECK (basis IN ('activity','spend')),
  tco2e             numeric(16,6) NOT NULL,
  release_batch_id  uuid NOT NULL REFERENCES release_batch(id),
  idempotency_key   text NOT NULL UNIQUE,             -- blocks double-release
  carbon_ref        text NOT NULL,                    -- 'CARB-...'
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_entry_client_batch ON emission_entry(client_id, release_batch_id);

-- hash-chained audit trail (SHARED)
CREATE TABLE audit_event (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id    uuid NOT NULL REFERENCES client(id),
  entity_type  text NOT NULL,                          -- 'claim','release_batch'
  entity_id    uuid NOT NULL,
  event_type   text NOT NULL,                          -- 'submitted','classified','edited','approved','released','tsa_anchored','reversed'
  actor        text NOT NULL,                          -- user or 'system'
  detail       jsonb,
  prev_hash    text,
  hash         text NOT NULL,
  ip           text,
  device       text,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_entity ON audit_event(entity_type, entity_id);
```

**Multi-tenant seam (later):** add `firm_id` to `client` and scope every query by `firm_id`; do not add now, but keep services parameterised so it slots in.

---

## 4. Carbon classification (shared `carbon` module)

Reuse the exact logic already in the ERP Sync engine. Factors below are **demo placeholders (D14)** and live in `emission_factor`.

| factor_key | scope | unit | kgCO₂e/unit |
|---|---|---|---|
| fuel_diesel | 1 | L | 2.68 |
| fuel_petrol | 1 | L | 2.31 |
| electricity | 2 | kWh | 0.585 |
| natural_gas | 1 | m³ | 2.03 |
| air_travel | 3 | km | 0.18 |

Rules:
- Map `expense_type` → `factor_key`. `other` (or no match) → no factor.
- **Activity-based:** if a matching factor exists **and** `quantity > 0` → `tco2e = quantity × factor_kg_per_unit / 1000`, `basis='activity'`, `data_quality='Activity-based'`, `scope` from the factor.
- **Spend-based fallback:** otherwise → `tco2e = total_amount × SPEND_FACTOR / 1000` (placeholder `SPEND_FACTOR = 0.35` kgCO₂e/RM, D14), `basis='spend'`, `data_quality='Spend-based — lower data quality'`, scope = factor scope if known else 3.
- Always record `factor_version` actually used.

---

## 5. OCR provider

`OcrProvider.extract(image_bytes, media_type) -> Extraction`. `AnthropicVisionProvider` calls `claude-sonnet-4-6` with the image and this instruction, returning **only** JSON:

```
keys: vendor (string), doc_no (string|null), date (string|null), currency (string|null),
total_amount (number|null), expense_type ("fuel_diesel"|"fuel_petrol"|"electricity"|
"natural_gas"|"air_travel"|"other"), quantity (number|null), unit ("L"|"kWh"|"m3"|"km"|null),
confidence (number 0..1).
Rules: fuel pump receipt -> fuel_diesel/fuel_petrol by product (RON95/97=petrol; diesel/B7/B10=diesel),
quantity = litres. Electricity bill (e.g. Tenaga Nasional/TNB) -> electricity, quantity = kWh.
Strip thousands separators. null where not printed.
```
Strip any ```json fences, parse, validate with a pydantic model. On parse/HTTP failure: surface a clean "couldn't read" error and keep the claim unsaved (no partial). **Tests mock this provider — never call the API in CI.** Real OCR-vendor selection is deferred (D4).

---

## 6. API (FastAPI)

| Method · path | Does |
|---|---|
| `POST /api/claims/upload` | multipart image → store to disk + sha256 → OCR → classify → insert `claim` (status `in_review`) + `audit_event('submitted')` → return claim with extracted + carbon |
| `GET /api/claims?status=` | list claims for the client |
| `GET /api/claims/{id}` | claim detail |
| `PATCH /api/claims/{id}` | edit extracted fields → re-classify → `audit_event('edited')` (only while not released) |
| `POST /api/claims/{id}/approve` | `in_review` → `approved` + `audit_event('approved')` |
| `POST /api/claims/{id}/release` | `approved` → `released`: create `release_batch` (hash), `emission_entry` (idempotent), `audit_event('released')` + `audit_event('tsa_anchored')` (stub token). All-or-nothing in one DB transaction. |
| `GET /api/ledger` | `emission_entry` rows + Scope 1/2/3 + total tCO₂e (computed in SQL) |
| `GET /api/audit/{claim_id}` | the hash-chained events for a claim |

---

## 7. Web UI (simple, real — reads/writes the API)

Three pages, server-rendered (Jinja) or a small static front; reuse the visual language from `02_eclaim.html` (green `#2d6a4f`, dark `#123030`, cream `#f4f8f6`).
1. **Capture** — drag/drop or pick a receipt → uploads → routes to Review.
2. **Review** — source image + extracted fields (editable) + carbon classification (scope, factor, tCO₂e, data-quality flag) → Approve, then Release.
3. **Ledger** — released records from the DB with Scope 1/2/3 totals and each record's `carbon_ref` + batch hash.

---

## 8. Release gate (shared `release` module)

- **Batch hash** = SHA-256 of canonical, key-sorted JSON of the released entry/entries (claim id, scope, factor_key, factor_version, quantity, tco2e). Reproducible from stored rows.
- **Idempotency:** `emission_entry.idempotency_key = sha256(client_id + claim_id)`. Re-release of the same claim must **not** create a second entry (DB unique constraint enforces it).
- **Irreversibility:** a `released` claim and its `emission_entry` are immutable — no edit, no delete by any role. Correction = a **reversing entry** (negative tco2e, `event_type='reversed'`), never an in-place change.
- **TSA:** `tsa_token` is a stub (e.g. local timestamp id) now; the real RFC 3161 call replaces the stub later without schema change.

---

## 9. Config, migrations, run

- `.env`: `DATABASE_URL=postgresql+psycopg://user:pass@host:5432/onecapture`, `ANTHROPIC_API_KEY=...`, `TEST_DATABASE_URL=...`.
- `alembic upgrade head` creates all tables; a seed script inserts the demo `emission_factor` rows and one `client`.
- `uvicorn` serves the API + UI.

---

## 10. Tests (against a test Postgres DB; OCR mocked)

1. Upload + mocked OCR → correct scope/factor/tCO₂e for diesel (activity), electricity (activity), and a no-quantity doc (spend-based, flagged).
2. Release writes exactly one `emission_entry`, one `release_batch`, and a continuous `audit_event` chain (each `prev_hash` links).
3. Re-release of the same claim creates **zero** new entries (idempotency).
4. A released claim cannot be edited or deleted (immutability); correction creates a reversing entry.
5. `GET /api/ledger` Scope 1/2/3 totals equal the sum of entries.
6. Batch hash is deterministic and recomputes from stored rows.

---

## 11. Acceptance for this build

Done when: a real receipt uploaded through the UI is read, classified, reviewed, approved, and released into Postgres; the ledger reflects it with correct scope and tCO₂e; the audit chain traces the carbon figure back to the source image hash; re-release is idempotent; and all tests pass against a Postgres test database. ERP Sync continues to pass unchanged, now writing to the same database.
