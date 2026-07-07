# e-Claim — Production Readiness Audit (CTO Review)

> ## STATUS UPDATE — 2026-07-06 (verified, not just claimed)
>
> All Part-1 + Part-2 items were committed (923fc92..655e3cc) and then
> independently re-verified by adversarial review, incl. mutation testing and
> live-DB probes. Suite: **354 passed, 0 skipped** on RLS-enforced Postgres.
>
> | Item | Verified status |
> |------|-----------------|
> | B2 append-only ledger/audit | ✅ DONE (live-DB verified, 8 pinning tests) |
> | B3 no duplicate claims on retry | ✅ DONE (DB UNIQUE, crash-window closed) |
> | B4 poison-job cap + dead-letter | ✅ DONE |
> | B5 SoD denial txn | ⚠️ implemented correctly but NOT test-pinned |
> | B6 CSRF | ✅ DONE (full web surface, API correctly exempt) |
> | B7 upload caps | ⚠️ PARTIAL (413-vs-400 gap; regresses 100 MB ZIP flow) |
> | Release race (UNIQUE + FOR UPDATE) | ✅ DONE (e-Claim side; erpsync loser 500s) |
> | Image decompression guard | ⚠️ PARTIAL (stitch resize-before-check hole) |
> | Login rate-limit | ⚠️ PARTIAL (API /auth/login untested) |
> | App A attestation | ⚠️ PARTIAL (JSON API + /capture/mileage bypass it) |
> | App A duplicate detection | ✅ DONE |
> | App B approval matrix | ⚠️ PARTIAL (approvals_required NOT enforced) |
>
> ### ROUND-2 VERIFICATION — 2026-07-07
> P1–P7 + Appendix D re-verified adversarially (8 verifiers + regression
> critic, mutation-tested). Suite: **381 passed, 0 skipped**. Verdicts:
> P3, P4, P5, P6, P7, Appendix D — ✅ DONE. P1, P2 — ⚠️ PARTIAL (small
> residuals only). Regression risk: LOW. **C1 in progress; C2 green-lit.**
>
> ### ROUND-3 POLISH LIST (small; do alongside C1/C2, before any pilot)
> **R1 (from P1).** Legacy `approval_matrix_rule` rows with
> `approvals_required>1` are not migrated, and `sod.py:89-90`
> `_describe_rule` still renders "2× a partner" in SoD denial messages /
> audit reasons for them. Add a data migration clamping legacy rows to 1 and
> drop the "N×" prefix until Phase-2.
> **R2 (from P3, critic MEDIUM).** Pre-P3 out-of-pocket claims with NULL
> attestation are permanently un-releasable (400) — no re-attest path. Add a
> re-attest action (or one-time backfill migration for existing rows).
> **R3 (from Appendix D, critic MEDIUM).** The erpsync leaf derives from
> `category != 'UNMAPPED'` but release selects by STATUS: an approved-as-is
> UNMAPPED row posts while showing a dash; a dismissed mapped row still says
> "posts on release". Derive the leaf from releasability (status in
> clean/approved + mapped), or gate approve-as-is on a mapping; suppress the
> tooltip for dismissed rows.
> **R4 (from P2).** Body cap == ZIP budget exactly (zero headroom for
> multipart/ZIP framing — boundary uploads 413) and no at-scale multipart
> test. Add ~1 MiB headroom to `max_upload_mb` and keep the invariant test
> as cap >= budget + headroom.
> **R5 (LOW nits).** erpsync remap form: require `factor_value` client-side
> (bare 422 alert otherwise); comment the latent streamed-response edge in
> `api/limits.py:74-87`; note upload-concurrency/proxy cap as a B8 deploy item.
>
> ### ORIGINAL ROUND-1 PUNCH LIST (all addressed; kept for history)
> **P1 — approvals_required is a fake control (HIGH).** The Growing/Enterprise
> templates write `approvals_required=2` ("two partners above 10k") but
> `sod.py:33-40` never reads it — ONE partner approves. Either enforce the count
> (needs partial-approval state) or REMOVE `>1` rows from launch templates + hide
> the promise in the UI. A promised-but-unenforced control is worse than none.
> **P2 — upload-cap regression (HIGH).** The new 50 MB body cap
> (`config.py:80`) is below the documented ZIP budget (50 entries / 100 MB,
> `ingestion.py:53-55`) — bulk ZIPs that worked now 413. Raise `max_upload_mb`
> ≥ 100 or clamp the ZIP budget and document it.
> **P3 — attestation bypass (HIGH).** `POST /api/claims/upload`
> (`api/routes.py:65-95`) and legacy `POST /capture/mileage`
> (`web/routes.py:528-591`) create out_of_pocket claims with NULL attestation and
> no downstream gate. Require/record `attested` on both (or gate approve/release
> on attestation for out-of-pocket claims). Add tests.
> **P4 — image-bomb holes (MEDIUM).** `documents.py:116` resizes each page
> BEFORE the stitch pixel check (huge alloc possible via extreme-aspect pages);
> `render_pdf_pages` clamps area not aspect; segmenter `_thumbnail` swallows
> `ImageTooLarge` (`segment.py:96-97`). Check-before-resize, clamp aspect, add
> HEIC + segmenter bomb tests.
> **P5 — pin B5 with tests (MEDIUM).** Mutation testing shows NO test fails if
> the sod.py commit fix is reverted. Add: denial event survives request-session
> rollback; unrelated pending work does NOT persist on denial.
> **P6 — B7 correctness (MEDIUM).** Streamed over-cap bodies surface as
> FastAPI 400 "error parsing the body", not the intended 413
> (`limits.py:57-64` vs fastapi routing); add a real-stack over-cap test.
> **P7 — smaller items (LOW):** rate-limit test for `/auth/login` +
> Retry-After on web 429; erpsync `release_clean` IntegrityError → idempotent
> no-op (not 500); reverse-collision test; add `approval_matrix_rule` to
> `test_rls_enforcement.py` DATA_TABLES; pin viewer-block on `/admin/approvals`;
> erpsync review queue buttons use bearer fetch from a cookie-only browser
> session (401) — switch that surface to cookie auth + CSRF.
>
> **Launch remains gated on the owner actions:** rotate secrets, real IdP,
> TLS/hosting, CI, PDPA + pen test. Those are unchanged below.

**Question asked:** Is the e-Claim system ready to publish for real users?
**Verdict:** **NO — not yet. Do NOT open it to paying users in its current state.**
**But:** the core is genuinely well-built. This is a "finish the last mile to
production" job, **not** a rewrite. Clearing the blockers below is realistic.

Date of audit: 2026-07-05. Method: 5 parallel deep-dive audits (auth/tenancy,
data integrity/ledger, HTTP attack surface, OCR/ingestion/worker, tests/ops)
plus a full test run (294 passed, 0 skipped).

---

## 1. The honest headline

You have a **strong engineering foundation** wrapped in a **dev-grade deployment
with several production gaps**. The gaps are the normal "productionisation"
work every system needs before real users — they are specific and fixable.

### What is genuinely strong (do not disturb)
- **Multi-tenant isolation** — Postgres Row-Level Security, *forced*, with the app
  connecting as a real unprivileged role, fail-closed if misconfigured, and
  default-deny. This is the best part of the system and it is well-tested.
- **Separation of duties** (maker ≠ checker) — enforced at BOTH the database
  (CHECK constraint) and the service layer. Denied attempts are audited.
- **Money & carbon math** — `Decimal`/`Numeric` end-to-end, no float coercion.
- **Migrations** — clean linear chain 0001→0018, every one reversible.
- **Input safety** — no SQL injection, no XSS/template injection, no
  mass-assignment. OCR model output is parsed defensively. ZIP upload handling is
  hardened (zip-bomb + path-traversal covered). Files stored by content hash.
- **294 tests pass**, including real Postgres RLS/SoD/isolation tests.

### Why it still can't launch
The blockers cluster in four areas: **(a) there is no real login for
production**, **(b) the "immutable ledger / tamper-proof audit" is only enforced
in Python, not the database**, **(c) crash/retry can create duplicate financial
claims**, and **(d) there is no production deployment (no TLS, no CI, single
Windows machine).** Details below.

---

## 2. BLOCKERS — must fix before ANY real user

| # | Blocker | Where | Why it blocks launch |
|---|---------|-------|----------------------|
| B0 | **Live API keys + DB passwords sit in the on-disk `.env`** (Anthropic, Google Maps, Postgres). Git-ignored and never committed — but real and now exposed. | `.env` | Rotate all of them **today**. Independent of everything else. |
| B1 | **No working production login.** Dev login is passwordless (identity-only); the real SSO provider is a `NotImplementedError` stub. | `auth/provider.py:60-85`, `auth/routes.py` | In prod nobody can log in; in dev *anyone can log in as anyone*. No auth = no launch. |
| B2 | **Ledger & audit trail are not append-only at the DB level.** The app role has `UPDATE`/`DELETE` on `emission_entry`, `carbon_handoff`, `audit_event`, `release_batch`. Immutability is Python-only. | `alembic/…/0002_multitenant_spine.py:168,177` | The core selling point ("tamper-evident carbon ledger") is not actually tamper-evident. `REVOKE UPDATE,DELETE` or add reject-triggers. |
| B3 | **Crash/retry can create DUPLICATE financial claims.** Job completion isn't idempotent: if the process dies after the claim commits but before the job is marked done, the job is re-claimed and a second claim is built. | `services/ingestion.py:507`, `ingest/worker.py:133` | Duplicate money + carbon events in normal deploy/restart scenarios. Key the built claim to `job_id`. |
| B4 | **Poison-job infinite loop.** The `attempts` counter is incremented but never checked, so a job that reliably crashes the worker is retried forever (re-billing OCR each time). | `ingest/worker.py:36-45,70` | One bad upload can loop forever and burn API spend. Enforce `attempts < max` + dead-letter. |
| B5 | **`commit()` inside the SoD approval guard** breaks the "one request = one atomic transaction" contract. | `services/sod.py:79` | Partial persistence / a 403 that still commits work. Use a separate transaction/savepoint. |
| B6 | **No CSRF protection** on any cookie-authenticated state-changing route (approve, release, reject, admin). | `web/routes.py` POST handlers | An attacker page can make a logged-in reviewer silently approve/release a claim. Add CSRF tokens or Origin/Referer checks. |
| B7 | **No upload size limit** — request body read fully into memory. | `api/routes.py:80`, `web/routes.py:308,230` | A large upload OOMs the single host. Cap body size + file count. |
| B8 | **No production deployment.** Runs as a Windows Scheduled Task on one machine, uvicorn on plain HTTP/127.0.0.1, no TLS, no reverse proxy, no replicas; a failed migration doesn't even stop startup. | `scripts/serve.ps1:46`, `install-autostart.ps1` | Not deployable to real customers. Containerise behind a TLS proxy; abort on migration failure. **Deploy note (from R4/R5):** the app-level body cap (`api/limits.py`, `max_upload_mb`) buffers each request in memory on a single worker, so the reverse proxy must ALSO cap upstream body size (at/above `max_upload_mb`) AND bound concurrent large uploads (e.g. `client_max_body_size` + a connection/upload-concurrency limit) — otherwise N simultaneous full-budget uploads still exhaust host memory even though each one is individually within the cap. |
| B9 | **No CI; DB-backed security tests silently skip when Postgres is absent.** They passed locally, but nothing enforces they keep running. | `tests/eclaim/conftest.py:105,130,143`; no `.github/` | Your RLS/SoD/audit safety net can silently disappear. Add CI with a mandatory Postgres service (fail, not skip). |
| B10 | **No PDPA / data-protection posture.** Receipt images (personal data) are stored unencrypted on local disk; no retention/deletion policy, no data-residency decision, no pre-launch pen test. | `config.py` (`IMAGE_DIR`), specs | Legal exposure onboarding real Malaysian PII. Encrypt at rest, define retention, resolve residency, pen-test. |

---

## 3. HIGH — fix before scaling / shortly after a limited pilot

- **Release concurrency races.** Two simultaneous `/release` calls can double-release
  (zero-carbon claims have no idempotency backstop) and fork the audit chain. Add
  `UNIQUE(client_id, batch_hash)` and `SELECT … FOR UPDATE` the claim; map
  `IntegrityError` → idempotent no-op. (`services/claims.py:1163`, `models.py:398`)
- **Image decompression-bomb guard.** No `Image.MAX_IMAGE_PIXELS`; a crafted image/PDF
  can blow up memory. (`services/documents.py:53,71,84`)
- **No login rate-limiting / brute-force protection** anywhere. (`web/routes.py:564`)
- **Unbounded OCR cost/fan-out.** No per-tenant quota; a user can drive dozens of paid
  vision calls per upload, repeatedly. (`services/ingestion.py:62`)
- **In-process worker architecture.** One worker thread per web process; deploys kill
  in-flight jobs; you can't scale OCR independently. Fine for a single-host pilot;
  decouple (separate worker/queue) before horizontal scaling. (`api/app.py:26-40`)
- **No observability.** No `/healthz`/`/readyz`, no structured logging, no request IDs,
  no error tracking (Sentry-equivalent), no metrics. Prod is a black box.
- **No token revocation.** Stolen token valid until expiry; logout only clears the
  cookie. Add `jti` + deny-list, invalidate on logout/deactivation/role change.
- **Incomplete `.env.example`** — omits required vars (`APP_DATABASE_URL`, `JWT_SECRET`,
  `ENVIRONMENT`, maps key…); an operator following it gets an app that won't boot.
- **Auth token sign/verify/expiry is untested.** Add negative tests (forged sig,
  mutated payload, expired). (`auth/tokens.py`)
- **SSRF/cost-amplification** on the Google Maps endpoints (user-driven server-side
  fetches, no rate limit). (`maps.py`)
- **No backups / runbook / DR.**
- **LHDN over-claim risk.** The v1 "evidence pack" is honest but is NOT the Phase-3
  tax-audit pack, and its TSA timestamp is a stub. Don't market "LHDN-ready" or
  "anchored" until real TSA + GL trail exist.

---

## 4. MEDIUM / LOW — cleanups (track, not launch-blocking)

- SQL-side pagination for list/ledger/audit endpoints (currently load-all-in-Python).
- Magic-byte file-type sniffing (don't trust client `content_type`).
- Security headers (CSP, X-Content-Type-Options, X-Frame-Options).
- Stop echoing upstream/exception detail to users; add a generic 500 handler.
- `reverse()` idempotency should check the full line-key set, not just the first.
- `tip_hash` fork-safety; wire `verify_chain` into an integrity endpoint.
- Assert `alg == "HS256"` on token verify; pass `principal` to the API `resubmit` route.
- Surface partial-OCR-failure summary on the review screen; sanitise `job.error`.
- Constrain OCR-extracted amounts to `>= 0` and a sane max.

---

## 5. Suggested sequence

**Phase 0 — today:** rotate all secrets in `.env` (B0).

**Phase 1 — launch blockers (a limited pilot gate):** B1 (real login), B2 (DB
immutability), B3/B4 (idempotent + bounded jobs), B5 (SoD txn), B6 (CSRF),
B7 (upload cap), B8 (containerise + TLS + migration gate), B9 (CI), B10 (PDPA
basics + pen test).

**Phase 2 — before you scale beyond a pilot:** the HIGH list — concurrency races,
rate limiting, OCR quotas, worker decoupling, observability, token revocation,
backups/runbook.

**Phase 3 — ongoing hardening:** the MEDIUM/LOW cleanups.

Rough order-of-magnitude: Phase 1 is a few focused weeks of engineering **plus**
external lead-time items (real IdP integration, TLS/hosting, a third-party pen
test, a PDPA policy decision) that you should start in parallel now because they
don't depend on code.

**Bottom line:** solid core, not yet shippable. Fix the Phase-1 blockers and this
becomes a credible limited-pilot launch; the tenancy/audit/release foundation is
sound enough to build on rather than replace.

---

# Appendix A — Product design note: verifying "out-of-pocket" payments

> This is a **product enhancement**, NOT a launch blocker. It addresses the
> question: *how do we make sure an expense marked "out of pocket" was really
> paid by the employee themselves, so we don't reimburse money they never spent
> (by mistake or fraud)?*

## Decision: do NOT collect credit-card / payment-card details

Collecting card numbers to "prove" payment is the wrong tool:

1. **It doesn't prove anything** — a card number doesn't prove that person paid,
   nor that the card is personal vs corporate.
2. **It creates serious compliance liability** — storing a card number (PAN) puts
   the system in **PCI-DSS** scope and raises **PDPA** exposure, for near-zero
   benefit. **Never store a full card number.**
3. **It's high-friction** — asking every claimant for card details on every claim
   hurts adoption to solve a problem a checkbox solves better.

The real risk is narrow: *"do we owe this employee money?"* Answer it with cheap
controls first, heavy controls only when the amount justifies it.

## The layered, risk-based approach (implement in this order)

**Layer 1 — Attestation checkbox (BUILD THIS — highest value, lowest friction).**
Add ONE mandatory checkbox at claim submission:
> *"I confirm these out-of-pocket expenses were paid with my own money and have
> not been (and will not be) reimbursed elsewhere."*
- Block submission unless ticked (for any claim containing an `out_of_pocket`
  line). Record who attested + timestamp; include it in the evidence pack.
- Where: capture/submit flow (`web/templates/capture.html`, submit handler in
  `services/claims.py` / `services/ingestion.py`); persist an `attested_by` +
  `attested_at` (or an audit event `attested`) on the claim. Surface it on the
  review screen (`web/templates/review.html`) and in the evidence PDF
  (`services/evidence_pdf.py`).

**Layer 2 — Approver sign-off (ALREADY EXISTS).**
The separation-of-duties approval flow already makes a manager accountable. Keep.

**Layer 3 — Automatic duplicate detection (BUILD THIS — cheap, catches real abuse).**
The most common actual abuse is the *same receipt claimed twice*. You already
store `image_sha256` on every line — use it:
- On submit, flag (don't hard-block) when a line's `image_sha256` — or the tuple
  (vendor, total_amount, doc_date) — matches an existing line for the same
  client, including across the ERP-Sync and e-Claim channels.
- Surface the match to the approver as a warning on review. Today duplicate
  detection is only a manual approver note (`services/claims.py:89`) — automate it.

**Layer 4 — Proof of payment, threshold-gated (BUILD THIS — configurable).**
Only for out-of-pocket lines above a per-firm amount threshold, require a
**photo attachment** of proof of payment: a card slip, or a bank / e-wallet
transfer screenshot (FPX / DuitNow / Touch 'n Go / GrabPay are natural in MY).
- Treat it as an ordinary image attachment (reuse the existing image pipeline),
  NOT structured card fields.
- If a card number is ever visible on a slip, capture **last 4 digits only** as
  free-text evidence — never the full PAN (keeps it out of PCI scope).
- Make the threshold a per-client setting (e.g. `Category.default_limit`-style),
  so a RM 15 taxi needs no proof but a RM 2,000 hotel does.

**Layer 5 — Corporate-card reconciliation (LATER — the gold standard).**
If the company issues corporate cards, match claims against the card transaction
feed so a corporate-card spend simply cannot be claimed as out-of-pocket.
Powerful but heavy (needs a bank/card feed integration) — Phase 2+, not launch.

## Summary for the builder

- ✅ Add: **attestation checkbox** (Layer 1) + **automatic duplicate detection via
  `image_sha256`** (Layer 3). These two cover ~90% of the real risk with almost
  no user friction — do them first.
- ⏭ Add configurable: **threshold-gated proof-of-payment image** (Layer 4).
- 🚫 Do NOT: collect or store full credit-card numbers, or ask for payment
  details on every claim.
- 🔒 If any card digits are captured from a proof image: **last 4 only**, as
  evidence text, never the full number.

---

# Appendix B — Approval authority matrix (configuration, not customization)

> **Strategy:** e-Claim is being sold to existing CarbonNext customers of ALL
> sizes (SME up to ~3,000 employees) as the data-capture front end. It must be
> **one complete, self-service product** — every customer runs the same code and
> only changes **settings**. NO bespoke code per customer, ever.

## The governing rule for the whole team

> **Configuration, not customization.** Every customer need becomes a *setting*.
> If a request can't be met by configuration, you either extend the config model
> (for everyone) or you say no. Never add a per-customer code branch.

The completeness lives in the **rule engine + data model**, NOT in the UI. Build
the model fully flexible now; ship a simple UI now and a richer UI as a
fast-follow — one engine underneath, forever.

## Data model — build this to be future-proof (single-tier AND multi-layer)

One tenant-scoped table (RLS like the other data tables). This single shape
supports single-approval today and multi-layer approval chains later **with no
schema change** — the difference is just how many rows a band has.

**`approval_matrix_rule`**

| Column | Purpose |
|--------|---------|
| `id` | PK |
| `firm_id`, `client_id` | tenant scope + RLS |
| `scope_department` (nullable) | NULL = applies to all departments; else per-dept override |
| `scope_category_id` (nullable, FK category) | NULL = all categories; else per-category override |
| `min_amount`, `max_amount` (nullable) | the amount band; `max_amount` NULL = unlimited |
| `step_order` (int, default 1) | 1 = first approval; 2, 3… = additional layers (multi-level) |
| `approver_role` (nullable) | required role: `manager` / `partner` / … |
| `approver_user_id` (nullable, FK app_user) | OR a specific person (advanced) |
| `approvals_required` (int, default 1) | e.g. "any 2 partners" at this step |
| `active` (bool) | soft-disable a rule |
| `created_at`, `updated_at` | audit |

**How the same table serves both worlds:**
- **Single-tier (launch):** one row per band, `step_order = 1`. The engine reads
  only step 1 → one approval. This covers the large majority of companies.
- **Multi-layer (fast-follow):** multiple rows share a band with `step_order`
  1, 2, 3 → sequential approvals. Optional `scope_department` / `scope_category_id`
  and `approver_user_id` cover enterprise needs. **No migration needed** to turn
  this on — just write more rows.

**Engine (`services/sod.py`):** replace the flat per-user check with a matrix
lookup — given the claim's `total_claimed` (and dept/category if set), find the
matching band, load its steps in `step_order`, and require each step's
role/count. Keep `AppUser.authority_limit` as an OPTIONAL extra personal cap on
top. NOTE: true multi-step approval needs new claim states (e.g.
`pending_step_2`) — that state machine is the Phase-2 part; the launch engine
only evaluates `step_order = 1`.

## UI — phased, progressive disclosure (protects the simple experience)

**Phase 1 (launch): Wizard → Template → editable table.**
- A short setup **wizard** asks 3–4 plain questions (NOT "number of customers"):
  1. How many employees submit claims? (scale)
  2. Typical and maximum claim amount? (sets thresholds)
  3. One sign-off, or must large amounts go higher? (single vs tiered)
  4. Who approves the large amounts? (a role)
- The answers select a **named template** (below). The template is written as
  real, **editable** rows into `approval_matrix_rule` — SME accepts as-is; anyone
  can tweak. Lives under the existing firm-scope `/admin/*` area.
- Only single-tier (`step_order = 1`) is exposed here.

**Phase 2 (fast-follow, same engine): Advanced mode behind an "Advanced" toggle.**
- Add steps per band (multi-layer), per-department/category scoping, person-based
  approvers. 95% of customers never open it → the SME UI stays simple. This is a
  standard product feature, self-service — NOT white-glove, NOT bespoke.

## Starter templates (seed row-sets; amounts are adjustable defaults, MYR)

| Template | Profile | Rule (editable after apply) |
|----------|---------|------------------------------|
| **Starter** | micro, <20 staff | any amount → `manager`, 1 approval |
| **Small business** | 20–100 | ≤ 2,000 → `manager`; > 2,000 → `partner` |
| **Growing** | 100–500 | ≤ 1,000 → `manager`; 1,000–10,000 → `partner`; > 10,000 → `partner` ×2 |
| **Enterprise** | 500+ | as Growing + per-department scoping + multi-step (Phase 2 advanced) |

Templates are just preset row-sets the wizard writes — no separate table needed.

## "Everything is a setting" — the config surface a new customer self-serves

To truly avoid customization, ALL of these must be admin-configurable (no code):
- ✅ Approval matrix (this appendix) — per company
- ✅ Categories → emission factor / GL export code (exists: `category` table)
- ✅ Per-company authority limits & proof-of-payment threshold (Appendix A)
- ✅ Payment methods & reimbursement rule (exists)
- ✅ The company ↔ CarbonNext link (`client.carbonnext_company_id`)

If all of these are self-service in the admin UI, a new CarbonNext customer
onboards e-Claim themselves with zero bespoke work.

## Summary for the builder

- Build the **`approval_matrix_rule` table + engine** now, with the full column
  set above (so multi-layer needs no future migration).
- Wire `services/sod.py` to read the matrix (band → step 1 → role/count); keep
  `authority_limit` as an optional personal cap.
- Phase 1 UI: **wizard + template picker + simple single-tier table editor** under
  `/admin`. Phase 2: **Advanced multi-layer editor** behind a toggle (same engine).
- Multi-step approval *states* (`pending_step_2`, escalation) are Phase 2 — the
  launch engine evaluates `step_order = 1` only.
- 🚫 Never add a per-customer code path. Extend the config model for everyone, or
  say no.

---

# Appendix C — Unified intake & AP Invoice module (Phase 3 bridge)

> **Strategy:** users must NEVER choose "is this an e-Claim or an AP invoice?"
> before uploading. One capture inbox; the system classifies each document and
> routes it; the user only confirms. e-Claim = money the employee already spent
> (reimbursement / petty cash). AP Invoice = a vendor bill the company still owes
> (e.g. construction: DO + invoice for paint/sand → finance pays the vendor).
> Same front door, different back rooms.

## C0. Architecture

```
        ┌─────────────── ONE CAPTURE INBOX ───────────────┐
        │  upload (photo / PDF / ZIP) → OCR + CLASSIFIER   │
        │        → document_type + confidence              │
        └───────────────────────┬──────────────────────────┘
                    ┌───────────┴───────────┐
              expense_claim              ap_invoice
                    │                        │
             e-Claim module          AP Invoice module (NEW)
             (exists)                 vendor, coding, 3-way match
                    │                        │
        internal reimbursement      ERP CONNECTOR (pluggable seam)
                    │                 ├── SAP B1 (Service Layer REST)
                    │                 ├── AutoCount (.NET SDK, on-prem)
                    │                 └── SQL Account (SDK, on-prem)
                    └──────────┬───────────┘
                        CarbonNext (both channels feed it)
```

Everything below the inbox REUSES the existing core: tenancy/RLS, audit chain,
image store (sha256), OCR pipeline, ingestion worker, category/carbon config,
CarbonNext handoff, and the Appendix-B approval engine (scoped per module).

> **STATUS 2026-07-07: C1 ✅ and C2 ✅ implemented** on `production-readiness-blockers`
> (migrations 0025/0026; `services/routing.py`, `intake.py`, `ap.py`, `erp.py`; intake
> holding queue + AP invoice workflow + CSV export; 427 tests pass). ERP posting is a
> CSV stub (C3 seam defined, no live connector yet). Carbon handoff for AP lines: the
> `category_id` is captured on each AP line, the release/handoff wiring is a follow-on.

## C1. Classifier + router (build FIRST — independent of any ERP work)

Extend the existing Claude-vision OCR extraction (`ocr/base.py` Extraction,
`ocr/anthropic_provider.py`) to also return:
- `document_type`: `expense_receipt` | `vendor_invoice` | `delivery_order` | `unknown`
- `type_confidence`: 0..1
- `type_signals`: list of strings (the cues used — for the review UI + audit)

Signals that separate the classes (prompt the model for these explicitly):
- **Bill-To** naming the company + an external vendor letterhead → vendor_invoice
- Keywords: "Tax Invoice", "Delivery Order", "Payment Terms", "Due Date",
  "Bank Account" → AP side; "Receipt", "Cash/Change", card slip → expense
- **PO / DO reference present** → AP (construction pattern)
- Payment terms / due date present → AP (a receipt is already paid)

Routing rules:
- High confidence (≥ threshold, default 0.85) → auto-route into the right queue,
  visibly labeled, with a one-click "this is actually a …" correction.
- Low confidence → a two-choice prompt at review: *"Something you paid and want
  back?"* vs *"A vendor bill for finance to pay?"*
- EVERY routing decision (auto or manual, and corrections) is an audit event.
- A DO and its matching invoice should link (same vendor + PO/DO ref) — store the
  link; the DO alone is not payable.

Persistence: add `document_type`, `type_confidence`, `routed_to`,
`routed_by` (system|user), to the intake record; a corrected route re-runs the
right builder. Until the AP module ships, `vendor_invoice` documents land in a
visible **"Vendor bills (coming soon)" holding queue** — capture now, process
later; do NOT silently force them into e-Claim.

## C2. AP Invoice domain module (workflow first, ERP stubbed)

New tenant-scoped tables (RLS like the rest):
- **`vendor`**: firm_id, client_id, name, tax_id (SST/e-Invoice TIN), bank
  details (for payment reference — NOT card data), erp_vendor_code (nullable
  until mapped), status. Unique (client_id, erp_vendor_code).
- **`ap_invoice`** (header): firm_id, client_id, vendor_id, doc_no, doc_date,
  due_date, payment_terms, currency, subtotal/tax/total (Numeric, Decimal
  end-to-end), po_ref, do_ref, image/pages provenance (sha256, reuse claim_line
  pattern), status: `captured → coded → pending_approval → approved → posted →
  paid` (+ `held`, `rejected`), erp_doc_entry (nullable — the ERP's key after
  posting), idempotency_key UNIQUE.
- **`ap_invoice_line`**: description, quantity, uom, unit_price, line_total,
  gl_code, tax_code, category_id (carbon), cost dims (department/project).

Rules:
- Approval reuses the **Appendix-B matrix engine** with a module scope
  (`module: eclaim | ap`) — vendor bills usually need different bands. Add the
  `scope_module` column to `approval_matrix_rule` NOW (nullable, NULL = all) so
  no migration is needed later.
- SoD: the person who coded/keyed an AP invoice cannot approve it (same
  maker≠checker pattern, DB CHECK + service guard).
- Duplicate detection: same vendor + doc_no (+ amount) is the classic
  double-payment fraud — flag hard here (stronger than e-Claim's soft flag).
- Carbon: AP lines flow to CarbonNext through the same category/handoff pattern
  as e-Claim (raw activity data; CarbonNext computes).
- **ERP posting is a stub in this phase**: a clean `ERPConnector` seam + a
  manual **CSV/export file** per ERP format so accountants can post manually
  from day one. This proves the whole workflow with zero integration risk.

## C3. ERP connector seam (then ONE real connector)

```python
class ERPConnector(Protocol):
    def push_ap_invoice(inv) -> PostResult      # returns erp_doc_entry / error
    def pull_vendors() -> list[VendorRef]
    def pull_gl_accounts() -> list[GLRef]
    def pull_open_pos(vendor) -> list[PORef]    # for 3-way match (C4)
```

- Same discipline as the CarbonNext seam: idempotency key per push, ack/receipt
  + `erp_doc_entry` recorded, retries with backoff, posting failures land in a
  visible retry queue — never silently lost.
- **SAP B1** = Service Layer (REST/JSON, cloud-reachable if hosted) — cleanest
  first target. **AutoCount / SQL Account** = Windows SDKs against LOCAL
  installs → require a small **on-prem connector agent** (installed at the
  customer, outbound-only polling to the cloud app — no inbound firewall holes).
  Budget the agent as its own deliverable (installer, auto-update, health ping).
- NEVER write directly to any ERP's database — SDK/API only.
- Pick the FIRST connector by what existing CarbonNext customers actually run
  (survey them); push-only first, pull second.

## C4. Later: pull + 3-way match

Pull vendor master / GL / open POs; match Invoice ↔ DO ↔ PO with tolerances;
exceptions queue. Construction wants this — but it is enterprise AP; do not
build before one connector is live and stable.

## Sequencing & gates

| Step | Depends on | Note |
|------|-----------|------|
| C1 classifier + router | e-Claim Part-1 fixes landed (they touch the same ingestion files) | Small; high UX value immediately |
| C2 AP domain + CSV export | C1; Appendix-B engine merged | Full AP workflow, zero ERP risk |
| C3 first real connector (+ on-prem agent if AutoCount/SQL) | C2; customer ERP survey | The hard part; ONE ERP only |
| C4 pull + 3-way match | C3 stable in production | Enterprise AP |

🚫 Same governing rule as Appendix B: configuration, not customization. A new
ERP is a new connector implementing the same Protocol — never a customer fork.

---

# Appendix D — Carbon-related flag + leaf icon (owner decision, 2026-07-07)

> **Owner decision:** OneCapture does NOT own emission computation. CarbonNext
> computes CO2e. OneCapture's job is to *differentiate* — is this transaction
> carbon-related or not? Carbon-related lines are what get posted to CarbonNext
> on release. Every listing must show this with a **leaf icon column**.

## What this means per module

- **e-Claim — already built this way.** Since migrations 0009/0011, e-Claim does
  no carbon math: `claim_line.carbon_relevant` (snapshotted from the category at
  classify time) decides what is forwarded via `carbon_handoff`; CarbonNext
  computes tonnage. The flag exists — it is just NOT yet surfaced on listings
  (only in `admin_categories.html`).
- **ERP Sync — derive the flag; do NOT rip out the calculator now.** A staged
  row is carbon-related iff a mapping rule matched (`category != 'UNMAPPED'`).
  The internal tCO2e calc stays for now — it is baked into the audited batch-hash
  contract and the ledger, and the release path was just verified; treat its
  tCO2e as an internal estimate, not the product output. Full alignment (drop or
  demote erpsync tonnage) happens when the CarbonNext payload schema is
  confirmed (see CarbonNext_Integration_Requirements.txt §3 — now answered:
  raw activity data + category; CarbonNext computes).

## UI change (small, no migration needed)

Add a **carbon column with a leaf icon** to every listing:

| Screen | Rows | Leaf shown when |
|--------|------|-----------------|
| `web/templates/claims.html` | claim list | any line has `carbon_relevant` |
| `web/templates/review.html` | line table | that line `carbon_relevant` |
| `erpsync review erpsync_queue.html` | staged rows | `category != 'UNMAPPED'` |
| `erpsync review erpsync_entry.html` | entry detail | same rule |

Implementation notes:
- Icon: small inline SVG leaf, green (`--text-success`-style), with tooltip +
  `aria-label`: **"Carbon-related — posts to CarbonNext on release"**. Non-carbon
  rows show a muted dash (not an empty cell, so the column reads as deliberate).
- e-Claim claim list: compute the any-line flag in the existing list query (no
  N+1 — aggregate in SQL).
- Column header: a leaf glyph with a legend line under the table (or header
  tooltip) — do not label it "CO2e" (we are not showing a number).
- Tests: one per screen asserting the leaf renders for a carbon-relevant row
  and the dash for a non-carbon row.
