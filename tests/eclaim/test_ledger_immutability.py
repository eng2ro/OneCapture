"""B2 — the ledger/audit tables are append-only at the DATABASE level.

The app connects as the unprivileged ``onecapture_app`` role. Immutability used
to be Python-only; migration 0019 revokes UPDATE/DELETE so a bug or a compromised
app process cannot rewrite or erase released carbon/audit records. Corrections are
appended (a reversing row), never mutated.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

# The four append-only tables (see migration 0019).
LEDGER_TABLES = ["emission_entry", "carbon_handoff", "audit_event", "release_batch"]


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_app_role_keeps_insert_select_but_not_update_delete(db_engine, table):
    """Privilege check via the owner connection (runs whenever the DB is up):
    onecapture_app keeps SELECT + INSERT (still writes new rows) but has lost
    UPDATE + DELETE on every ledger/audit table."""
    with db_engine.connect() as c:
        def has(priv: str) -> bool:
            return c.execute(
                text("SELECT has_table_privilege('onecapture_app', :t, :p)"),
                {"t": table, "p": priv},
            ).scalar_one()

        assert has("SELECT") is True, f"{table}: app role must still read"
        assert has("INSERT") is True, f"{table}: append-only means INSERT stays"
        assert has("UPDATE") is False, f"{table}: UPDATE must be revoked"
        assert has("DELETE") is False, f"{table}: DELETE must be revoked"


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_app_role_update_and_delete_actually_error(app_engine, table):
    """End-to-end: as the real app role, an UPDATE or DELETE against the ledger
    raises (InsufficientPrivilege → SQLAlchemy ProgrammingError), even for a
    no-op WHERE — permission is checked at the table, so a released row cannot be
    tampered with or erased."""
    for stmt in (
        f"UPDATE {table} SET id = id WHERE false",   # would rewrite a row
        f"DELETE FROM {table} WHERE false",          # would erase a row
    ):
        with app_engine.connect() as c:
            with pytest.raises(ProgrammingError) as exc:
                c.execute(text(stmt))
            assert "permission denied" in str(exc.value).lower()
