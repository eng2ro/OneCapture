# OneCapture — Multi-tenant + Identity + SoD Spine

**Phase 1 foundation spec · authoritative for this build task**

Build this **before** any further module work. Both Hub and e-Claim sit on it, and the existing e-Claim backend is single-tenant — it must be retrofitted onto this spine **without breaking its 52 passing tests**.

This spec governs the build; it implements the architectural anchors in the OneCapture v1.2 requirements (`every table is multi-tenant: firm_id + client_id`; SoD `submitter ≠ approver` enforced at the API; tenant isolation `verified by automated tests, not inspection`; Carbon Next as the downstream destination).

---

## Decisions (already made — do not re-litigate)

- **Postgres Row-Level Security (RLS)** is the primary isolation mechanism, with app-layer scoping as a second line. Reason: the assurance gate (D5) and bank clients need isolation provable by an automated test at the DB, not dependent on never forgetting a `WHERE` clause.
- **UUID v4 primary keys** for all new tenancy tables. `firm_id` / `client_id` are UUID columns added to existing tables regardless of their current PK type.
- **Pluggable auth**: a `DevAuthProvider` (local signed-token login) now, with an `EntraOIDCProvider` seam for later. Do **not** block this spine on real Entra ID.
- **OneCapture identity is separate from CarbonNext.** OneCapture serves the accountant firm + claimants (who do not exist in CarbonNext). The two products meet only at the ingestion boundary (IR-6), where OneCapture posts as a single service identity ("company_dataentry"). Do not share or import CarbonNext's user directory.

---

## 0. Read from the repo first

Before writing code, inventory and report: the existing e-Claim SQLAlchemy models and their table names; `core.audit` / `core.release` / `core.carbon`; the Alembic env/config; the test fixtures and how they build a DB session; and the DB role behind `DATABASE_URL` / `TEST_DATABASE_URL`. Target the retrofit at the real names you find — do not assume the names below.

---

## 1. Tenancy model

`firm_id` (plus `client_id` where the row belongs to a client) on every tenant-scoped table.

- **Firm** = the accountant practice (owns firm-wide users).
- **Client** = the company whose claims/invoices are processed.
- One firm → many clients.
- Roles: **Partner, Manager** = firm scope (all clients in their firm); **Approver, Viewer** = client scope (only granted clients); **Submitter** = virtual, no account.

---

## 2. New tables

- **firm** — `id`, `name`, `status`, `created_at`.
- **client** — `id`, `firm_id` FK, `name`, `status`, `modules` (jsonb flags: eclaim/erpsync/ap/ar enabled), `whatsapp_number`, **`carbonnext_company_id`** (the CarbonNext company this client maps to — match CarbonNext's company-id type; nullable until mapped; **unique**: one OneCapture client ↔ one CarbonNext company), `created_at`. Tenant key: `firm_id`. Treat CarbonNext's company registry as the source of truth — reference it here, don't re-key the company master.
- **app_user** — `id`, `firm_id` FK, `entra_object_id` (nullable), `email`, `display_name`, `base_role` enum(partner|manager|approver|viewer), `authority_limit` numeric (nullable), `status`, `created_at`.
- **user_client_grant** — `id`, `user_id` FK, `client_id` FK, unique(`user_id`,`client_id`). Only client-scoped users need grants; Partner/Manager implicitly access all firm clients.
- **claimant** — `id`, `firm_id`, `client_id`, `name`, `phone` (WhatsApp identity, unique within client), `email`, `employee_ref`, `cost_centre`, `status`. **No credentials** — identity is channel binding.

---

## 3. Identity & authentication

- **Firm users** authenticate through an `AuthProvider` interface. Implement `DevAuthProvider`: a `/auth/login` endpoint that verifies a seeded user and mints a signed session token (JWT or server session) carrying `user_id` + `firm_id` + `base_role`. Stub `EntraOIDCProvider` behind the same interface for Phase-2 wiring.
- **Submitters never authenticate.** Add `resolve_claimant(firm_id, client_id, channel_value)` that matches phone/email to the claimant master; unknown sender → return a quarantine signal (the intake module surfaces it; never silently drop). That is all the spine owns for claimants.

---

## 4. Request principal + tenant context

- FastAPI dependency `get_principal()`: validate token → load user, `firm_id`, `base_role`, and the resolved `allowed_client_ids` (all firm clients if firm-scoped; the grant set if client-scoped).
- At transaction start for each request, set two Postgres session vars: `SET LOCAL app.current_firm = <firm_id>` and `SET LOCAL app.allowed_clients = '<comma-joined client uuids>'`. Wire this via a SQLAlchemy session dependency so every query in the request runs under tenant context.

---

## 5. Enforcement — defense in depth

- **RLS on every tenant table.** Policy pattern:
  `firm_id = current_setting('app.current_firm', true)::uuid AND (client_id IS NULL OR client_id = ANY(string_to_array(current_setting('app.allowed_clients', true), ',')::uuid[]))`.
  Unset context → NULL → zero rows (deny by default).
- **Critical gotcha:** table owners and superusers **bypass RLS**. So: (a) create a dedicated `onecapture_app` Postgres role — `LOGIN`, **no** `SUPERUSER`, **no** `BYPASSRLS` — grant it DML only; (b) the app and the tests connect as `onecapture_app`; (c) migrations run as the admin/owner role; (d) still `ALTER TABLE ... FORCE ROW LEVEL SECURITY` on every tenant table as belt-and-suspenders. Add `APP_DATABASE_URL` (onecapture_app) for the app/tests; keep the existing admin DSN for Alembic.
- **App-layer scoping** as the second line: a tenant-aware base repository/query that always injects `firm_id`/`client_id` filters, so the app is correct even outside RLS.
- **SoD at the API.** On claims, track `created_by_user_id`, `submitted_by_claimant_id`, `approved_by_user_id`. The approval service rejects when `approved_by == created_by` (a firm user who keyed a claim can't approve it), when the approver lacks a grant to that client, or when amount exceeds `authority_limit`. Add a row-level DB CHECK `(approved_by_user_id IS NULL OR approved_by_user_id <> created_by_user_id)` as a second layer (amount/grant checks stay in the service — they're dynamic).

---

## 6. CarbonNext link (IR-6 service identity — "company_dataentry")

- **Config/secrets:** add `CARBONNEXT_API_URL` and `CARBONNEXT_SERVICE_TOKEN` to settings/.env. This one token is OneCapture's service identity into CarbonNext — the company_dataentry principal. One identity, not per-company.
- **Interface:** add a `CarbonNextClient` (place per repo convention; `integrations/carbonnext.py` is natural) exposing `post_emission_entries(carbonnext_company_id, batch_id, idempotency_key, entries) -> ack`. For this spine, implement it as a **stub** (no live HTTP) so the mapping, credential, and interface have a home; the real call ships with the release/ingestion module.
- **Target resolution:** at post time the destination company is resolved per batch from `client.carbonnext_company_id`. A client with no mapping cannot be posted — raise or queue, never guess.

---

## 7. Alembic migration (ordered, non-breaking)

1. Create the five new tables (§2).
2. Seed one default firm + one default client (use "ABC Manufacturing" to match the mockups) so existing data has a home.
3. Add `firm_id` (nullable) + `client_id` (nullable) to existing e-Claim tables.
4. Backfill all existing rows to the default firm/client.
5. Set columns `NOT NULL`, add FKs + indexes on `firm_id` and `client_id`.
6. Create `onecapture_app` role + DML grants; `ENABLE` + `FORCE` RLS and create policies on all tenant tables.
7. Add the SoD CHECK constraint.

Provide a real `downgrade()` (drop policies/role grants → drop columns → drop tables). Run against both `onecapture` and `onecapture_test`.

---

## 8. Tests

- Update fixtures to seed a firm + client + one user per role, plus a **second firm and client** for isolation tests, connect as `onecapture_app`, and set tenant context per test.
- New tests (must be automated, not inspection):
  - **Isolation:** an Approver at client A gets zero rows / 403 for client B — assert at the API *and* by direct DB query under firm-A context against firm-B rows.
  - **Cross-firm:** a firm-1 user cannot see any firm-2 data.
  - **SoD:** submitter-equals-approver blocked at the API (403) *and* the DB CHECK rejects `approved_by == created_by`.
  - **Role scope:** Viewer cannot approve; Approver over `authority_limit` is blocked; a client-scoped user with no grant to client X gets nothing; a Partner sees all firm clients but no other firm.
  - **CarbonNext mapping:** an unmapped client (no `carbonnext_company_id`) raises on a post attempt against the `CarbonNextClient` stub.
  - The existing **52 e-Claim tests** pass within the seeded tenant context.

---

## 9. Non-goals / seams (do not over-build)

- No real Entra (DevAuthProvider only).
- No UI — backend spine only.
- Claimant *intake* logic (FR-E1) is a separate module — here, just the table + resolver.
- The full approval-tier routing ladder (FR-E4) is e-Claim's job — here, just `base_role` + `authority_limit` + the SoD guard.
- The actual CarbonNext wire-up (live HTTP, ack handling, retry/backoff, reconciliation per IR-6) is built with the release/ingestion module — the spine only lands the `carbonnext_company_id` mapping, the service-credential config, and the `CarbonNextClient` stub.

---

## 10. Done when

All tests green including the new isolation + SoD suite; the app connects as a non-superuser role; an isolation test proves that querying another tenant's rows under the wrong context returns empty at the DB layer; and an unmapped client raises on a post attempt.
