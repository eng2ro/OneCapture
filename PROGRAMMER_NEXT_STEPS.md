# OneCapture e-Claim — Next steps for the programmer

Goal: **host e-Claim standalone** and **link it to CarbonNext**. This is the
concrete task list, with the exact files to work in. Read `HANDOFF.md` first for
setup and architecture; the design detail behind every integration item is in
`eClaim_Production_Readiness_Audit.md` (Appendices F, H, I) and
`CarbonNext_Integration_Requirements.txt`.

State today: the e-Claim app is **feature-complete and UAT-ready** (611 tests
pass). What's left is **production hosting** + the **real CarbonNext hook-up** —
both are designed with clean seams but not built.

---

## TRACK 0 — Run e-Claim standalone (drop the ERP-Sync module)

The repo also contains a separate **ERP Sync** module. The e-Claim app currently
mounts it too. To run **e-Claim only**:

- In `src/eclaim/api/app.py`, remove/comment the two ERP-Sync router includes
  (the `erpsync_api_router` / `erpsync_web_router` lines). e-Claim then runs on
  its own via `uvicorn eclaim.api.app:app`.
- The ERP-Sync DB tables can stay (harmless) or be excluded — they're independent
  of e-Claim. No e-Claim code depends on ERP Sync.

Everything else (`/capture`, `/claims/*`, `/approvals`, `/payables`, `/admin/*`,
`/ledger`, `/coverage`) **is** the e-Claim module.

---

## TRACK 1 — Hosting / production deployment

A checklist to take it off localhost. None of this needs the CarbonNext side.

1. **Server + Postgres.** A Linux VPS/cloud host + a managed PostgreSQL. Create
   the app database; the first migration creates the `pgcrypto` extension (needs
   suitable rights).

2. **Two Postgres roles — this matters for security.** RLS is **FORCED**. The app
   must connect at runtime as an **unprivileged** role (not the table owner /
   superuser), or RLS is bypassed and tenants leak.
   - `DATABASE_URL` = admin/owner DSN → used ONLY for `alembic upgrade head` + seed.
   - `APP_DATABASE_URL` = unprivileged app-role DSN → what the running app uses.
   Grant the app role `SELECT/INSERT/UPDATE/DELETE` on the tables (not ownership),
   so the RLS policies apply to it.

3. **Production `.env`.** `ENVIRONMENT=production`, a strong random `JWT_SECRET`
   (the app refuses to start in production with the default), `session_cookie_secure`
   on, real `ANTHROPIC_API_KEY` and `GOOGLE_MAPS_API_KEY` (restricted keys, active
   Google Cloud billing + Routes API enabled), `CARBONNEXT_*` when Track 2 lands.

4. **Real authentication — REQUIRED for production.** Login is passwordless dev
   mode today; `DevAuthProvider` **refuses to run when `ENVIRONMENT=production`**.
   Implement one of (owner to decide):
   - `EntraOIDCProvider` in `src/eclaim/auth/provider.py` (currently a
     `NotImplementedError` seam) — Microsoft Entra / OIDC SSO, or
   - **"Sign in with CarbonNext"** OAuth (if CarbonNext can be the IdP — preferred
     since users already exist there), or
   - local password hashing + reset flow (adds a `password_hash` column + email).
   The `AuthProvider` Protocol is already the seam; only the provider body changes.

5. **TLS + reverse proxy.** Put nginx/Caddy in front for HTTPS on a real domain.

6. **Process manager.** Run uvicorn/gunicorn workers under **systemd** (Linux)
   instead of the Windows Scheduled Task (`scripts/serve.ps1` is Windows-only).
   `alembic upgrade head` on each deploy.

7. **Seed real data, not the demo.** Do NOT run `scripts/seed_uat.py` in prod.
   Create the real firm + company + first admin user (a small prod seed, or the
   activation flow in Track 2).

8. **Ops.** Nightly Postgres backups, uptime + error monitoring, and **budget
   alerts** on the Anthropic + Google keys (both bill per call).

---

## TRACK 2 — Link to CarbonNext

The seam exists; the wire does not. e-Claim already builds a `carbon_handoff` row
per carbon-relevant line on release — the work is to actually POST them and to
build the surrounding contract.

**Prereq:** get the CarbonNext side to answer `CarbonNext_Integration_Requirements.txt`
(payload schema, auth, sites API, activation contract, reversal disposition).

1. **Implement the client.** `src/eclaim/integrations/carbonnext.py` →
   `CarbonNextClient.post_emission_entries(...)` is a **stub** today (it just
   records the call and returns a fake `STUB-CARBONNEXT:` receipt). Replace the
   body with a real HTTP POST to `CARBONNEXT_API_URL` authenticated with
   `CARBONNEXT_SERVICE_TOKEN`, sending each record's `captured_by` attribution.
   Keep it **idempotent** (the batch already carries an idempotency key / per-line
   `carbon_ref`).

2. **Wire it into release.** In `src/eclaim/services/claims.py` `release(...)`,
   after the `carbon_handoff` rows are written, call the client to forward that
   batch. A CarbonNext outage must **not** block a release — write the handoff
   rows in the transaction, then post out-of-band with retry (a small outbox/queue
   the release doesn't wait on). Record the ack/receipt against the batch.
   Do the same for the **AP channel** (vendor invoices currently build no handoff).

3. **Sites API (company → branch → site).** Sync the tenant's site hierarchy from
   CarbonNext (it owns it — multi-tenant) so the capture form can offer branch/site
   dropdowns and soft-validate that an invoice's bill-to belongs to the user's
   company. Design: Appendix H + F-E.

4. **Activation endpoint.** An authenticated endpoint CarbonNext calls to provision
   a company: create the firm/company + a locked-down **"CarbonNext Bridge"**
   service user (can post but never approve/pay/log-in interactively). Design:
   Appendix I. This is how prod companies get created instead of a manual seed.

5. **Reversal disposition.** When a released claim is reversed, how CarbonNext nets
   it — `accepted` / `pending_approval` / `adjusted_current_period`. Design:
   Appendix F-F (deliberately left as design until CarbonNext confirms the contract).

6. **Posting identity.** All posts authenticate as the company's service identity
   with per-record `captured_by` + human-readable remarks (Appendix I-C) — the
   `CarbonNextClient` already takes a `carbonnext_company_id`; map each company to it.

---

## Definition of done

- **Standalone e-Claim hosted** on HTTPS with real auth, real DB roles (RLS
  enforced), real API keys, and a real firm/company.
- **CarbonNext linked**: a released claim's carbon lines land in CarbonNext with a
  real receipt; reversals net correctly; new companies provision via activation.

Ground rules that kept this codebase healthy (please keep them): run the full test
suite before/after every change and pin each fix with a test; never weaken an
integrity rule (SoD, append-only ledger, post-approval lock, attestation) to make
something pass; design before touching money/approvals/ledger.
