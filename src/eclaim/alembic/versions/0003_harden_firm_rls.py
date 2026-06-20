"""harden firm-match RLS to nullif (empty current_firm → NULL → deny)

Revision ID: 0003_harden_firm_rls
Revises: 0002_multitenant_spine
Create Date: 2026-06-18

0002's client-match wraps the GUC in ``nullif(current_setting(...), '')`` so an
empty ``app.allowed_clients`` resolves to NULL and the policy denies. The
firm-match did NOT: it cast ``current_setting('app.current_firm', true)::uuid``
bare, so an empty-string firm context raises ``invalid input syntax for type
uuid: ""`` instead of denying — an inconsistency and a latent error path (a
forgotten / blanked context becomes a 500, not a clean zero-row deny).

This migration rebuilds every tenant policy (firm + firm-only + data tables) with
the firm cast wrapped the same way:
``nullif(current_setting('app.current_firm', true), '')::uuid``. RLS is already
ENABLEd/FORCEd by 0002 and stays untouched here — we only DROP/CREATE the
policies. The downgrade restores 0002's bare-cast expressions verbatim.
"""

from alembic import op

revision = "0003_harden_firm_rls"
down_revision = "0002_multitenant_spine"
branch_labels = None
depends_on = None

# Client-match is unchanged from 0002 (already nullif-guarded); repeated here so
# the rebuilt data policies are byte-identical to 0002 apart from the firm cast.
_CLIENT_MATCH = (
    "(client_id IS NULL OR client_id = ANY("
    "string_to_array(nullif(current_setting('app.allowed_clients', true), ''), ',')::uuid[]))"
)

_FIRM_ONLY_TABLES = ["client", "app_user", "user_client_grant"]
_DATA_TABLES = ["claim", "release_batch", "emission_entry", "audit_event", "claimant"]

# The only thing that changes: the firm-context cast.
_FIRM_CAST_HARDENED = "nullif(current_setting('app.current_firm', true), '')::uuid"
_FIRM_CAST_ORIGINAL = "current_setting('app.current_firm', true)::uuid"


def _policies_sql(firm_cast: str) -> str:
    """Rebuild every tenant policy with the given firm-match cast. DROP then
    CREATE; RLS enable/force from 0002 is left in place."""
    firm_match = f"firm_id = {firm_cast}"
    data_policy = f"{firm_match} AND {_CLIENT_MATCH}"
    lines: list[str] = []
    # firm: gated by its own id, not firm_id.
    lines += [
        "DROP POLICY IF EXISTS firm_tenant ON firm;",
        "CREATE POLICY firm_tenant ON firm FOR ALL "
        f"USING (id = {firm_cast}) WITH CHECK (id = {firm_cast});",
    ]
    for t in _FIRM_ONLY_TABLES:
        lines += [
            f"DROP POLICY IF EXISTS {t}_tenant ON {t};",
            f"CREATE POLICY {t}_tenant ON {t} FOR ALL "
            f"USING ({firm_match}) WITH CHECK ({firm_match});",
        ]
    for t in _DATA_TABLES:
        lines += [
            f"DROP POLICY IF EXISTS {t}_tenant ON {t};",
            f"CREATE POLICY {t}_tenant ON {t} FOR ALL "
            f"USING ({data_policy}) WITH CHECK ({data_policy});",
        ]
    return "\n".join(lines)


def upgrade() -> None:
    op.execute(_policies_sql(_FIRM_CAST_HARDENED))


def downgrade() -> None:
    op.execute(_policies_sql(_FIRM_CAST_ORIGINAL))
