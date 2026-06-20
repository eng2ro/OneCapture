"""Default-deny on the *application* path (not just the raw DB path).

``test_rls_enforcement`` proves the database isolates rows when queried over a
hand-built connection. This complements it one level up: a plain ORM ``Session``
on the ``onecapture_app`` engine — exactly how the app's ``get_db`` acquires its
session — returns **zero rows** from a tenant table when the request never set
tenant context. The 0002 migration commits a default firm + client, so this is a
real, committed row being hidden by RLS, not an empty table.

If this ever returns rows, the app would be one forgotten ``set_config`` away
from leaking every tenant's data — the failure mode the whole spine exists to
make impossible. Skips with the rest of the e-Claim DB suite when the
``onecapture_app`` DSN is unreachable (``app_engine`` fixture).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from eclaim.db.models import Client


def test_app_engine_denies_tenant_rows_without_context(app_engine):
    # Mirror the app's get_db: a plain Session on the onecapture_app engine, but
    # WITHOUT the per-request set_config the app would normally issue first.
    session = Session(bind=app_engine, future=True)
    try:
        rows = session.query(Client).all()
    finally:
        session.close()
    assert rows == [], (
        "tenant rows visible with no app.current_firm set — default-deny is "
        "broken on the application engine path"
    )
