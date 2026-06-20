# OneCapture · ERP Sync — import-mode pipeline (v1 / Phase 1)

ERP-first carbon capture for AP listings. This is **pass 1** of the ERP Sync
module from the OneCapture Startup Requirements v1.2: read a standardised AP
invoice listing exported from an ERP (CSV/XLSX), validate and classify every
row, map carbon-relevant lines through a **versioned rules engine**, resolve
quantities (activity-first, spend-based fallback), compute **tCO₂e** with exact
decimal math, screen **cross-channel duplicates** against e-Claim, and produce
the **deterministic batch hash** that the release gate later anchors.

Everything runs **offline against synthetic data**. No external services.

## Scope of this pass

| # | Capability | Spec FR |
|---|------------|---------|
| 1 | AP listing import + validation report + whole-batch commit | FR-S1 |
| 2 | Idempotent re-import by ERP doc id (file SHA-256 retained) | FR-S1 |
| 3 | Versioned mapping rules, item → vendor → GL precedence | FR-S3 |
| 4 | Quantity resolution: activity else spend-based fallback (DQ-flagged) | FR-S4 |
| 5 | Emission calc → tCO₂e (exact `Decimal`) | supports S6 |
| 6 | Deterministic batch hash for the release gate | FR-S6 |
| 7 | Cross-channel dedup (ownership matrix + doc-number match) | FR-S8 |

**Stubbed behind clean seams (not implemented here):** the review queue
(FR-S5), live ERP connectors (FR-S2), coverage/reconciliation (FR-S7), and the
*external* half of release — RFC 3161 TSA stamping and the Carbon Next post
(`release/gate.py` ships `StubTSA` / `StubSink`).

## Stack

Python 3.12 · `pydantic` v2 (typed models / validation) · `openpyxl` + stdlib
`csv` (no pandas — it float-coerces money) · stdlib `hashlib` (SHA-256) ·
`PyYAML` (config) · `pytest`. All money / quantity / tCO₂e math uses `Decimal`
end to end so audited figures never drift.

## Setup

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
# source .venv/bin/activate on POSIX
```

## Run an import

```bash
# generate the synthetic month + a malformed variant
python scripts/gen_synthetic.py

# import (prints the validation report + batch hash + total tCO₂e)
python scripts/run_import.py \
  --config config/clients/abc_manufacturing.yaml \
  data/synthetic/abc_month_2026_05.csv
```

Expected on the clean month: `clean=3 warning=3 duplicate=1 rejected=0`,
6 entries committed, one cross-channel duplicate held back, total **20.562 tCO₂e**.

Re-running the same file commits **0** (idempotency). The malformed listing
returns `rejected=1`, stages nothing (whole-batch policy), and exits non-zero.

> The JSON store at `data/<client>_store.json` persists committed line keys
> across runs. Delete it to start fresh.

## Run the tests

```bash
python -m pytest -q
```

37 tests cover validation, idempotency, rule precedence, quantity resolution,
exact emissions math, the deterministic hash, cross-channel dedup (incl. the
planted-duplicate acceptance check), and the end-to-end month.

## Layout

```
src/erpsync/
  domain/        enums + frozen pydantic models (the hashed contract)
  ingest/        reader (Decimal-safe), column presets, validation, batch hashing
  rules/         versioned ruleset loader + item→vendor→GL match engine
  quantity/      activity-vs-spend resolver (FR-S4)
  emissions/     factor registry + exact tCO₂e calculator
  dedup/         cross-channel ownership matrix + doc-number screen (FR-S8)
  release/       deterministic batch hash + TSA/Carbon-Next seams (FR-S6)
  persistence/   idempotency store (in-memory + JSON file; DB seam)
  pipeline.py    S1 → S3 → S4 → calc → S8 → S6 orchestration
  cli.py         erpsync-import entry point
config/          per-client preset / ruleset / factors / ownership + client config
scripts/         gen_synthetic.py, run_import.py
tests/           pytest suite
```

## Baked-in policy defaults

1. **Commit gate** — any `rejected` (malformed) row blocks the *whole* batch;
   nothing partial commits. `warning` rows commit (staged spend-based / flagged);
   idempotency `duplicate` rows are skipped; cross-channel duplicates are held back.
2. **Idempotency grain** — line key `(client_id, DocEntry, LineNum)`. A seen key
   re-imports as a duplicate.
3. **Batch hash input** — canonical sorted JSON of each committed entry
   (line key, category, scope, basis, quantity, factor + versions, tCO₂e, source
   snapshot hash) → SHA-256. Order-independent and reproducible from stored records.

## Seams for later phases

`release/gate.py` (`TimestampAuthority`, `ReleaseSink`) → real RFC 3161 + Carbon
Next. `persistence/store.py` → Postgres / multi-tenant. `dedup` `eclaim_doc_numbers`
→ live e-Claim view. `ingest` live ERP connectors (SAP B1 Service Layer) replace
the file reader without touching the rest of the pipeline.

---

# OneCapture · e-Claim — receipt capture (Postgres)

The second module of the same app: a receipt is uploaded, read by **Claude vision
OCR**, carbon-classified, reviewed/edited by a human, approved, then **released**
into an immutable, hash-chained emission ledger in Postgres. e-Claim and ERP Sync
share one database and the same carbon/release/audit logic via **`src/core/`**.

## Shared core (`src/core/`)

Factored out so both modules call one implementation, not two:

| Module | What it owns |
|--------|--------------|
| `core.carbon`  | exact Decimal `tco2e(units, factor)` — ERP Sync's calculator and e-Claim's classifier both use it |
| `core.release` | `canonical_hash()` (order-independent SHA-256) + `StubTSA` / `StubSink` seams |
| `core.audit`   | `chain_hash()` / `verify_chain()` — tamper-evident hash-chained events |

## Stack

FastAPI · SQLAlchemy 2 + psycopg 3 · Alembic · pydantic-settings · Jinja2 UI ·
Anthropic vision (`claude-sonnet-4-6`) for OCR. Money/emissions stay `Decimal`
/ `numeric`.

## Setup

```bash
.venv/Scripts/python -m pip install -e ".[eclaim,dev]"
cp .env.example .env          # then fill DATABASE_URL / TEST_DATABASE_URL / ANTHROPIC_API_KEY
```

You provide Postgres. Create two databases (e.g. `onecapture`, `onecapture_test`);
the `pgcrypto` extension is created by the migration (needs suitable rights).

```bash
alembic upgrade head          # build the schema (all shared + e-Claim tables)
python scripts/seed.py        # demo client + emission-factor library (D14 placeholders)
uvicorn eclaim.api.app:app --reload   # API + web UI on http://127.0.0.1:8000
```

Pages: `/` capture · `/claims/{id}/review` review/approve/release · `/ledger` ledger.

## Tests

```bash
python -m pytest -q
```

The DB-independent tests (ERP Sync, shared core, carbon classification) always
run. The 6 e-Claim end-to-end tests run against `TEST_DATABASE_URL` (schema built
from the Alembic migration, each test in a rolled-back transaction, **OCR mocked**);
they **skip** with a clear note if no Postgres test DB is reachable.

## Deferred (clean seams, not built)

Multi-tenant / SSO auth, WhatsApp + email intake, real RFC 3161 TSA, Carbon Next
post, object storage for images, notifications, and multi-tier approval — all
stubbed or single-step for now, per the spec.

## e-Claim layout

```
src/core/            shared carbon / release / audit primitives
src/eclaim/
  config.py          pydantic-settings (.env)
  db/                SQLAlchemy models + session
  alembic/           migrations (0001_initial = full schema)
  repositories.py    persistence seam (claims, factors, releases, audit, ledger)
  ocr/               OcrProvider interface + AnthropicVisionProvider
  services/          classify (carbon) · claims (lifecycle) · audit (chain)
  api/               FastAPI routers, deps (txn + OCR injection), schemas
  web/               Jinja templates + static (capture / review / ledger)
scripts/seed.py      demo client + factor library
tests/eclaim/        unit (classify) + e2e flow tests
```
