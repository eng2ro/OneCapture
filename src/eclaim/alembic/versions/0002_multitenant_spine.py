"""multi-tenant + identity + SoD spine

Revision ID: 0002_multitenant_spine
Revises: 0001_initial
Create Date: 2026-06-18

Retrofits the single-tenant e-Claim schema onto the firm/client tenancy spine
(multitenant_spine_spec.md). Non-breaking: new tenancy columns land nullable,
existing rows are backfilled to a seeded default firm/client, then the columns
are locked NOT NULL. RLS + the ``onecapture_app`` role enforce isolation; the
SoD CHECK is the second layer of the approver≠creator rule.

Migrations run as the admin/owner (superuser), which bypasses RLS — so the
backfill happens before policies are enabled, and the SECURITY DEFINER login
lookup runs with owner privileges. The app/tests connect as ``onecapture_app``
(non-superuser, no BYPASSRLS), for which RLS actually bites.
"""

from alembic import op

revision = "0002_multitenant_spine"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

# Fixed id for the seeded default firm so the backfill is deterministic.
DEFAULT_FIRM_ID = "11111111-1111-1111-1111-111111111111"

# RLS policy expressions. current_setting(..., true) returns NULL when unset →
# the comparison is NULL → row denied. nullif(...,'') turns an empty client list
# into NULL so string_to_array never chokes on '' when casting to uuid[].
_FIRM_MATCH = "firm_id = current_setting('app.current_firm', true)::uuid"
_CLIENT_MATCH = (
    "(client_id IS NULL OR client_id = ANY("
    "string_to_array(nullif(current_setting('app.allowed_clients', true), ''), ',')::uuid[]))"
)
_DATA_POLICY = f"{_FIRM_MATCH} AND {_CLIENT_MATCH}"

# Firm-scoped tables (firm/client/app_user/user_client_grant): only the firm
# gate. The client roster is readable firm-wide so principal bootstrap can list
# clients; client-scoped narrowing for those users is the app layer's job.
_FIRM_ONLY_TABLES = ["client", "app_user", "user_client_grant"]
# Data tables: firm + allowed-client gate (the strong row isolation).
_DATA_TABLES = ["claim", "release_batch", "emission_entry", "audit_event", "claimant"]


UPGRADE = f"""
-- 1. New tenancy tables -----------------------------------------------------
CREATE TABLE firm (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,
  status      text NOT NULL DEFAULT 'active',
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- 2. Seed the default firm (existing data's home) ---------------------------
INSERT INTO firm (id, name) VALUES ('{DEFAULT_FIRM_ID}', 'OneCapture Default Firm');

-- 3. Extend client into the spine tenant ------------------------------------
ALTER TABLE client
  ADD COLUMN firm_id               uuid,
  ADD COLUMN status                text NOT NULL DEFAULT 'active',
  ADD COLUMN modules               jsonb,
  ADD COLUMN whatsapp_number       text,
  ADD COLUMN carbonnext_company_id text UNIQUE;

CREATE TABLE app_user (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id          uuid NOT NULL REFERENCES firm(id),
  entra_object_id  text,
  email            text NOT NULL,
  display_name     text NOT NULL,
  base_role        text NOT NULL
                   CHECK (base_role IN ('partner','manager','approver','viewer')),
  authority_limit  numeric(14,2),
  status           text NOT NULL DEFAULT 'active',
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_user_firm_email UNIQUE (firm_id, email)
);
CREATE INDEX ix_user_firm ON app_user(firm_id);

CREATE TABLE user_client_grant (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id    uuid NOT NULL REFERENCES firm(id),
  user_id    uuid NOT NULL REFERENCES app_user(id),
  client_id  uuid NOT NULL REFERENCES client(id),
  CONSTRAINT uq_grant_user_client UNIQUE (user_id, client_id)
);
CREATE INDEX ix_grant_firm ON user_client_grant(firm_id);

CREATE TABLE claimant (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id       uuid NOT NULL REFERENCES firm(id),
  client_id     uuid NOT NULL REFERENCES client(id),
  name          text NOT NULL,
  phone         text,
  email         text,
  employee_ref  text,
  cost_centre   text,
  status        text NOT NULL DEFAULT 'active',
  CONSTRAINT uq_claimant_client_phone UNIQUE (client_id, phone)
);
CREATE INDEX ix_claimant_firm_client ON claimant(firm_id, client_id);

-- 4. Add tenancy + SoD columns to existing tables (nullable first) ----------
ALTER TABLE claim
  ADD COLUMN firm_id                  uuid,
  ADD COLUMN created_by_user_id       uuid REFERENCES app_user(id),
  ADD COLUMN submitted_by_claimant_id uuid REFERENCES claimant(id),
  ADD COLUMN approved_by_user_id      uuid REFERENCES app_user(id);
ALTER TABLE release_batch  ADD COLUMN firm_id uuid;
ALTER TABLE emission_entry ADD COLUMN firm_id uuid;
ALTER TABLE audit_event    ADD COLUMN firm_id uuid;

-- 5. Backfill existing rows to the default firm/client ----------------------
-- Seed a default client only if the install has none (fresh/test DB).
INSERT INTO client (firm_id, name, currency, status)
SELECT '{DEFAULT_FIRM_ID}', 'ABC Manufacturing', 'MYR', 'active'
WHERE NOT EXISTS (SELECT 1 FROM client);

UPDATE client SET firm_id = '{DEFAULT_FIRM_ID}' WHERE firm_id IS NULL;
UPDATE claim          c SET firm_id = cl.firm_id FROM client cl WHERE c.client_id = cl.id AND c.firm_id IS NULL;
UPDATE release_batch  b SET firm_id = cl.firm_id FROM client cl WHERE b.client_id = cl.id AND b.firm_id IS NULL;
UPDATE emission_entry e SET firm_id = cl.firm_id FROM client cl WHERE e.client_id = cl.id AND e.firm_id IS NULL;
UPDATE audit_event    a SET firm_id = cl.firm_id FROM client cl WHERE a.client_id = cl.id AND a.firm_id IS NULL;

-- 6. Lock the tenancy columns NOT NULL + FKs + indexes ----------------------
ALTER TABLE client         ALTER COLUMN firm_id SET NOT NULL,
                           ADD CONSTRAINT fk_client_firm FOREIGN KEY (firm_id) REFERENCES firm(id);
ALTER TABLE claim          ALTER COLUMN firm_id SET NOT NULL,
                           ADD CONSTRAINT fk_claim_firm FOREIGN KEY (firm_id) REFERENCES firm(id);
ALTER TABLE release_batch  ALTER COLUMN firm_id SET NOT NULL,
                           ADD CONSTRAINT fk_batch_firm FOREIGN KEY (firm_id) REFERENCES firm(id);
ALTER TABLE emission_entry ALTER COLUMN firm_id SET NOT NULL,
                           ADD CONSTRAINT fk_entry_firm FOREIGN KEY (firm_id) REFERENCES firm(id);
ALTER TABLE audit_event    ALTER COLUMN firm_id SET NOT NULL,
                           ADD CONSTRAINT fk_audit_firm FOREIGN KEY (firm_id) REFERENCES firm(id);
CREATE INDEX ix_client_firm ON client(firm_id);
CREATE INDEX ix_claim_firm  ON claim(firm_id);
CREATE INDEX ix_batch_firm  ON release_batch(firm_id);
CREATE INDEX ix_entry_firm  ON emission_entry(firm_id);
CREATE INDEX ix_audit_firm  ON audit_event(firm_id);

-- 7. SoD second layer: an approver may not be the claim's creator -----------
ALTER TABLE claim ADD CONSTRAINT ck_claim_sod
  CHECK (approved_by_user_id IS NULL OR approved_by_user_id <> created_by_user_id);

-- 8. Login lookup: SECURITY DEFINER so the unprivileged app role can find a
--    user by email before any tenant context exists (owner bypasses RLS).
--    Pinned search_path: a SECURITY DEFINER function must never resolve objects
--    through the caller's search_path (a caller-planted app_user in another
--    schema could otherwise hijack the lookup). pg_catalog first, then public.
CREATE FUNCTION auth_lookup_user(p_email text)
RETURNS TABLE (id uuid, firm_id uuid, base_role text, status text)
LANGUAGE sql SECURITY DEFINER STABLE
SET search_path = pg_catalog, public AS $$
  SELECT id, firm_id, base_role, status FROM app_user WHERE email = p_email;
$$;

-- 9. The unprivileged application role (LOGIN, no SUPERUSER, no BYPASSRLS) ---
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onecapture_app') THEN
    CREATE ROLE onecapture_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO onecapture_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO onecapture_app;
GRANT EXECUTE ON FUNCTION auth_lookup_user(text) TO onecapture_app;

-- 9b. Auto-cover future objects. The grants above only touch tables/sequences
--     that exist *now*; a later migration's CREATE TABLE would be invisible to
--     onecapture_app without re-granting. ALTER DEFAULT PRIVILEGES makes every
--     future table/sequence the owner creates in this schema readable/writable
--     by the app role. Existing sequences are granted explicitly (UUID PKs use
--     gen_random_uuid() today, but any future serial/identity needs this).
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO onecapture_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO onecapture_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO onecapture_app;
"""


def _rls_sql() -> str:
    lines = ["-- 10. Row-Level Security: enable + force + policies ----------------------"]
    # firm: scoped by its own id.
    lines += [
        "ALTER TABLE firm ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE firm FORCE ROW LEVEL SECURITY;",
        "CREATE POLICY firm_tenant ON firm FOR ALL "
        "USING (id = current_setting('app.current_firm', true)::uuid) "
        "WITH CHECK (id = current_setting('app.current_firm', true)::uuid);",
    ]
    for t in _FIRM_ONLY_TABLES:
        lines += [
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;",
            f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY;",
            f"CREATE POLICY {t}_tenant ON {t} FOR ALL "
            f"USING ({_FIRM_MATCH}) WITH CHECK ({_FIRM_MATCH});",
        ]
    for t in _DATA_TABLES:
        lines += [
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;",
            f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY;",
            f"CREATE POLICY {t}_tenant ON {t} FOR ALL "
            f"USING ({_DATA_POLICY}) WITH CHECK ({_DATA_POLICY});",
        ]
    return "\n".join(lines)


def _downgrade_sql() -> str:
    drops = ["-- drop policies + RLS"]
    for t in ["firm", *_FIRM_ONLY_TABLES, *_DATA_TABLES]:
        drops.append(f"DROP POLICY IF EXISTS {t}_tenant ON {t};")
        drops.append(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY;")
        drops.append(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY;")
    return "\n".join(drops)


DOWNGRADE_TAIL = """
-- Revoke-only: undo this migration's grants + drop the login lookup, but DO NOT
-- drop the role. onecapture_app is a cluster-global role shared by the
-- onecapture and onecapture_test databases; DROP ROLE would fail while the other
-- database still holds grants/owns objects (and would be wrong even if it
-- succeeded). Tear the role down out-of-band when decommissioning the cluster.
REVOKE EXECUTE ON FUNCTION auth_lookup_user(text) FROM onecapture_app;
REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM onecapture_app;
REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM onecapture_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM onecapture_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM onecapture_app;
REVOKE USAGE ON SCHEMA public FROM onecapture_app;
DROP FUNCTION IF EXISTS auth_lookup_user(text);

-- drop SoD CHECK
ALTER TABLE claim DROP CONSTRAINT IF EXISTS ck_claim_sod;

-- drop added columns from existing tables
ALTER TABLE audit_event    DROP COLUMN IF EXISTS firm_id;
ALTER TABLE emission_entry DROP COLUMN IF EXISTS firm_id;
ALTER TABLE release_batch  DROP COLUMN IF EXISTS firm_id;
ALTER TABLE claim
  DROP COLUMN IF EXISTS approved_by_user_id,
  DROP COLUMN IF EXISTS submitted_by_claimant_id,
  DROP COLUMN IF EXISTS created_by_user_id,
  DROP COLUMN IF EXISTS firm_id;
ALTER TABLE client
  DROP COLUMN IF EXISTS carbonnext_company_id,
  DROP COLUMN IF EXISTS whatsapp_number,
  DROP COLUMN IF EXISTS modules,
  DROP COLUMN IF EXISTS status,
  DROP COLUMN IF EXISTS firm_id;

-- drop new tables (FK order)
DROP TABLE IF EXISTS claimant;
DROP TABLE IF EXISTS user_client_grant;
DROP TABLE IF EXISTS app_user;
DROP TABLE IF EXISTS firm;
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    op.execute(_rls_sql())


def downgrade() -> None:
    op.execute(_downgrade_sql())
    op.execute(DOWNGRADE_TAIL)
