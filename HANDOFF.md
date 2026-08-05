# OneCapture e-Claim — Developer Handoff

_Last updated: 2026-07 · state: UAT-ready, 611 tests passing, live on the author's machine at `http://127.0.0.1:8000`._

This document is everything a new developer needs to take over OneCapture e-Claim:
what it is, how to run it, how it's built, what's done, and what's next.

> **Orientation prompt (optional):** if you use an AI coding assistant (e.g. Claude
> Code), paste this into it to get oriented:
> _"This is OneCapture e-Claim — a Python/FastAPI + PostgreSQL expense-claim &
> AP-capture app that feeds a separate carbon-accounting system called CarbonNext.
> Read HANDOFF.md, then README.md, then eClaim_Production_Readiness_Audit.md.
> Don't guess — answer from the code. The web UI + routes are in
> src/eclaim/web/, business logic in src/eclaim/services/, the DB models in
> src/eclaim/db/models.py, migrations in src/eclaim/alembic/versions/."_

---

## 1. What it is

OneCapture **e-Claim** is a working web application (not a mockup) that captures
staff **expense claims** and vendor **AP invoices**, runs them through a
verify → approve → release → pay workflow, and forwards the **carbon-relevant**
lines to a **separate** system called **CarbonNext** (which does the actual CO₂e
maths). e-Claim does no carbon calculation itself — it captures, classifies,
approves, and hands off clean data.

- Multi-tenant: one "firm" (the accounting practice) manages many "companies"
  (`client` in the code). Postgres **row-level security** isolates every tenant.
- Two capture channels: **e-Claim** (staff receipts / mileage) and **AP** (vendor
  bills). A classifier routes each uploaded document to the right one.
- The carbon hand-off is the product's reason to exist — see §7 and the
  `CarbonNext_Integration_Requirements.txt` doc.

## 2. Where the code is

- **GitHub:** https://github.com/eng2ro/OneCapture (owner account: `eng2ro`).
  Ask the current owner for repo access.
- `main` is the source of truth. All work merges to `main`.
- **Secrets are NOT in git** (`.env` is git-ignored) — you must get them from the
  owner or provision fresh ones (see §5).

## 3. Tech stack

Python **3.12** · **FastAPI** (API + server-rendered UI) · **SQLAlchemy 2** +
**psycopg3** · **Alembic** migrations (37 of them) · **Jinja2** templates ·
**PostgreSQL** with FORCED row-level security · **pytest** (611 tests).
External services: **Anthropic** vision model for receipt OCR;
**Google Maps Routes API** for mileage distances. Money/quantities use `Decimal`
end-to-end.

## 4. Local setup

You provide a PostgreSQL server. Then:

```bash
# 1. clone + virtualenv (Windows shown; POSIX: source .venv/bin/activate)
git clone https://github.com/eng2ro/OneCapture.git
cd OneCapture
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -e ".[eclaim,dev]"

# 2. two databases (the pgcrypto extension is created by the first migration)
#    e.g. createdb onecapture ; createdb onecapture_test

# 3. config: copy the template and fill it in (see §5 for the values)
cp .env.example .env

# 4. build the schema, seed demo data
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python scripts/seed.py       # firm + 1 company + categories + 2 users
.venv/Scripts/python scripts/seed_uat.py   # 50 employees + users + ~75 demo claims

# 5. run it
.venv/Scripts/python -m uvicorn eclaim.api.app:app --reload   # http://127.0.0.1:8000
```

Login is **passwordless in dev** — just the email. Demo accounts are in
`LOGIN_ACCESS.txt` (e.g. `partner@demo.test`).

**Windows auto-start (how the author runs it live):** `scripts/serve.ps1` launches
uvicorn with a respawn loop and logs to `logs/`; `scripts/install-autostart.ps1`
registers it as a Scheduled Task that starts at logon. Not required for dev.

## 5. Secrets / config you need (`.env`)

Pydantic settings; env var = the UPPERCASE field name. Get these from the owner or
create fresh ones. **Rotate the owner's keys before real production — they've been
in a local file.**

| Env var | What it's for | Required? |
|---|---|---|
| `DATABASE_URL` | admin Postgres DSN (migrations/seed; bypasses RLS) | yes |
| `TEST_DATABASE_URL` | test Postgres DSN | for tests |
| `APP_DATABASE_URL` / `APP_TEST_DATABASE_URL` | unprivileged app-role DSN (RLS enforced at runtime) | prod-grade |
| `ANTHROPIC_API_KEY` | receipt OCR (vision model) | for OCR |
| `GOOGLE_MAPS_API_KEY` | server-side mileage route distances (Routes API) | for mileage |
| `GOOGLE_MAPS_BROWSER_KEY` | browser autocomplete on the capture map | optional |
| `JWT_SECRET` | signs session tokens (must be changed for production) | yes |
| `ENVIRONMENT` | `dev` (passwordless login) or `production` (hardened) | yes |
| `CARBONNEXT_API_URL` / `CARBONNEXT_SERVICE_TOKEN` | the future CarbonNext hand-off (not wired yet) | later |
| `MILEAGE_RATE_PER_KM`, `SPEND_FACTOR`, `MAX_UPLOAD_MB`, … | tunables (have sane defaults) | no |

Notes: the **Google Maps key** needs an **active Cloud billing account** with the
**Routes API** enabled (a lapsed trial → `403 PERMISSION_DENIED`). The Anthropic
and Google keys **cost money per call** — set budget alerts.

## 6. Codebase map

```
src/eclaim/
  api/app.py            FastAPI app factory, exception handlers, middleware
  api/routes.py         JSON/bearer API (16 endpoints) + CSV export
  api/deps.py           auth dependencies, RLS tenant context, role gates
  web/routes.py         the whole cookie-authed web UI (76 routes) ← biggest file
  web/templates/*.html  Jinja2 pages (capture, review, admin_*, ...)
  services/             business logic (the real rules live here):
    claims.py           e-Claim lifecycle: capture→verify→approve→release→pay
    ap.py               vendor-invoice (AP) lifecycle
    sod.py              separation-of-duties + approval-matrix engine
    ingestion.py        upload pipeline (OCR, PDF/ZIP expand, classify, build claim)
    settings.py         per-company behaviour toggles (registry pattern)
    users.py            firm login-user admin
    coverage.py, fx.py, vehicles.py, intake.py, routing.py, documents.py, ...
  ocr/                  Anthropic vision provider + page segmentation
  db/models.py          all SQLAlchemy models (the schema in code)
  alembic/versions/     37 migrations (0001 → 0037)
tests/eclaim/           611 tests — run before every change
scripts/                seed.py, seed_uat.py, serve.ps1, install-autostart.ps1
```

**Run the tests** (do this before and after any change):
```bash
OC_DISABLE_INGEST_WORKER=1 .venv/Scripts/python -m pytest -q
```

## 7. Key concepts (read before changing anything)

- **Multi-tenant RLS**: every tenant table has a FORCED row-level-security policy
  keyed on `firm` + `client` GUCs set per request. The runtime app connects as an
  unprivileged role that RLS enforces; migrations/seed use an admin DSN.
- **Roles**: `partner` / `manager` (firm-wide) vs `approver` / `viewer`
  (per-company via grants). Admin pages are partner/manager only.
- **Integrity rules that are NEVER configurable**: separation-of-duties
  (maker ≠ checker, enforced by DB CHECK **and** service), the append-only ledger
  (UPDATE/DELETE revoked on `emission_entry`/`carbon_handoff`/`audit_event`/
  `release_batch`), the post-approval lock, attestation at release, and
  corrections-by-reversal (never edit released data).
- **Configurable behaviour** lives in `services/settings.py` (a per-company
  registry): e.g. `capture.submitter_verification`, `carbon.auto_reverse`,
  `fx.auto_prefill`. Add a control = one registry entry, no schema change.
- **The claim flow**: capture (OCR reads receipts / mileage) → the uploader
  verifies content (if submitter-verification on) → reviewer approves (gated by
  the per-company **approval matrix** + personal authority limit + SoD) → release
  (attestation gate; forwards carbon lines to CarbonNext; exports all lines to the
  ERP) → mark paid (payer ≠ maker).
- **CarbonNext hand-off**: on release, each carbon-relevant line becomes one
  `carbon_handoff` row carrying category, amount, quantity+unit, vendor, date,
  employee ref/name/position, vehicle type, etc. **e-Claim sends raw data;
  CarbonNext computes CO₂e.** The real API call is stubbed — see §8.

## 8. Current state & what's pending

**Done / working (UAT-ready):** capture (image + PDF), OCR, classify + route
(e-Claim vs AP vendor bills), submitter verification, approval matrix, SoD,
FX/exchange rates, mileage with route picker, employees register + capture
binding, users admin, audit trails, CSV export, coverage & ledger views. Live
with seeded UAT data. **611 tests pass.**

**Owner actions still open (not code):** rotate the API keys/secrets out of
`.env`; TLS + real hosting (currently localhost only); CI pipeline; a PDPA / pen
test; decide login: local passwords vs "Sign in with CarbonNext" (SSO). Login is
**passwordless dev mode** today — production needs a real credential path.

**Gated on the CarbonNext programmer** (see `CarbonNext_Integration_Requirements.txt`):
- the real CarbonNext client + wiring the AP-channel hand-off,
- the **sites API** (company → branch → site) for bill-to validation,
- the **activation** endpoint (CarbonNext provisions a company + its first user),
- the **reversal-disposition** contract (how a reversal nets in CarbonNext).
These are designed but intentionally **not built** until CarbonNext confirms.

## 9. Documents to read (in the repo)

1. **`README.md`** — stack + setup (also covers the ERP-Sync sibling module).
2. **`eClaim_Production_Readiness_Audit.md`** — the living design/source-of-truth:
   every subsystem, decision, and open item (Appendices A–I).
3. **`CarbonNext_Integration_Requirements.txt`** — the questions/answers needed
   from the CarbonNext side to finish the integration.
4. **`eclaim_claim_redesign_spec.md`, `eclaim_postgres_spec.md`,
   `multitenant_spine_spec.md`** — deeper design specs.
5. **`OneCapture_Startup_Requirements_v1.2.docx`** — the original product brief.
6. **`LOGIN_ACCESS.txt`** — dev/demo login accounts.

## 10. Ground rules that kept this codebase healthy

- **Run the full test suite before and after every change**; add a test that pins
  each fix. Never weaken an integrity rule to make a test pass.
- Work on a branch, merge to `main`. Keep commits scoped and described.
- When a bug is reported on one screen, **audit every screen that shares that
  feature** and fix them together — don't fix just the reported one.
- Design before building anything that touches money, approvals, or the ledger.
