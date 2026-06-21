"""ERP Sync pipeline → Postgres ``erpsync_entry`` staging (0004).

Two cuts, both against the real Postgres test DB (these SKIP cleanly when none is
reachable, via the shared ``db_engine`` / ``app_engine`` fixtures):

1. **Wiring + status projection** — the pipeline, given a ``PgStagingStore`` sink,
   lands EVERY accepted line into ``erpsync_entry`` with the right review status
   (clean / held / flagged), and a re-import is idempotent (ON CONFLICT on the
   ``(client_id, doc_entry, line_num)`` grain). This is the e2e import re-pointed
   from the JSON store at the tenant-scoped staging table.

2. **RLS isolation** — proves the *database* keeps one firm's staged rows out of
   another's when queried by the unprivileged ``onecapture_app`` role, exactly as
   ``test_rls_enforcement`` does for the e-Claim ledger tables. Seeding is on the
   RLS-bypassing owner connection and COMMITTED so the app-role connection sees it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from eclaim.db.models import Client, ErpsyncEntry, Firm
from erpsync.domain.enums import BatchStatus
from erpsync.persistence.pg_staging import PgStagingStore
from erpsync.persistence.store import Store
from erpsync.pipeline import run_import
from gen_synthetic import month_rows, write_csv


# --------------------------------------------------------------------------- #
# 1) Pipeline wiring + status projection
# --------------------------------------------------------------------------- #
def _count_by_status(session: Session, client_id: uuid.UUID) -> dict[str, int]:
    rows = session.execute(
        text(
            "SELECT status, count(*) FROM erpsync_entry "
            "WHERE client_id = :c GROUP BY status"
        ),
        {"c": client_id},
    ).all()
    return {status: n for status, n in rows}


def test_pipeline_stages_every_accepted_line_with_status(
    db_session, config, tmp_path
):
    """The synthetic month stages 7 accepted lines: the 6 committable rows split
    clean/flagged by data quality, plus the 1 cross-channel hit held back for
    review — held, not dropped."""
    ids = db_session.info["principal"]
    sink = PgStagingStore(db_session, firm_id=ids["firm"], client_id=ids["client"])

    listing = tmp_path / "month.csv"
    write_csv(listing, month_rows())

    result = run_import(listing, config, Store(), staging=sink)
    db_session.flush()

    assert result.batch_status is BatchStatus.STAGED
    # 3 clean (measured+mapped) + 3 flagged (unmapped/spend/DQ) + 1 held (FR-S8).
    assert _count_by_status(db_session, ids["client"]) == {
        "clean": 3,
        "flagged": 3,
        "held": 1,
    }


def test_restaging_same_month_is_idempotent(db_session, config, tmp_path):
    """Re-importing the same listing (fresh in-memory stores, so all 7 lines are
    re-derived and re-offered) inserts nothing the second time — the staging
    table's UNIQUE grain dedups via ON CONFLICT DO NOTHING."""
    ids = db_session.info["principal"]
    sink = PgStagingStore(db_session, firm_id=ids["firm"], client_id=ids["client"])

    listing = tmp_path / "month.csv"
    write_csv(listing, month_rows())

    run_import(listing, config, Store(), staging=sink)
    db_session.flush()
    after_first = db_session.execute(
        text("SELECT count(*) FROM erpsync_entry WHERE client_id = :c"),
        {"c": ids["client"]},
    ).scalar_one()
    assert after_first == 7

    # Second pass, brand-new store: every line is re-derived and re-staged, but the
    # DB has them all already → zero new rows, and sink.stage reports 0 inserted.
    inserted = sink.stage(
        [
            (entry, "clean")
            for entry in run_import(listing, config, Store()).entries
        ]
    )
    db_session.flush()
    assert inserted == 0
    after_second = db_session.execute(
        text("SELECT count(*) FROM erpsync_entry WHERE client_id = :c"),
        {"c": ids["client"]},
    ).scalar_one()
    assert after_second == 7  # unchanged


# --------------------------------------------------------------------------- #
# 2) RLS isolation on erpsync_entry
# --------------------------------------------------------------------------- #
def _seed_firm_with_staged_row(session: Session, label: str) -> dict:
    """One firm + client + a single staged ``erpsync_entry`` row, all stamped with
    the firm id. Unique-ish keys so a leftover row from a crashed run can't clash."""
    tag = uuid.uuid4().hex[:8]
    firm = Firm(name=f"ERPSync RLS Firm {label}")
    session.add(firm)
    session.flush()

    client = Client(firm_id=firm.id, name=f"ERPSync RLS Client {label}", currency="MYR")
    session.add(client)
    session.flush()

    session.add(
        ErpsyncEntry(
            firm_id=firm.id, client_id=client.id,
            doc_entry=f"DOC-{tag}", line_num=0,
            category="Fleet diesel", scope="scope_1", basis="activity",
            data_quality="measured", factor_value=2, factor_version="v",
            rule_version="v7", tco2e=1, source_hash=tag, status="clean",
        )
    )
    session.flush()
    return {"firm": firm.id, "client": client.id}


@pytest.fixture
def two_firms_staged(db_engine):
    """Seed two isolated firms each with one staged row (owner connection,
    COMMITTED so the app-role connection sees them); remove them in teardown."""
    owner = Session(bind=db_engine, future=True, expire_on_commit=False)
    made: dict = {}
    try:
        made = {
            "a": _seed_firm_with_staged_row(owner, "A"),
            "b": _seed_firm_with_staged_row(owner, "B"),
        }
        owner.commit()
        yield made
    finally:
        for entry in made.values():
            fid = entry["firm"]
            owner.execute(text("DELETE FROM erpsync_entry WHERE firm_id = :f"), {"f": fid})
            owner.execute(text("DELETE FROM client WHERE firm_id = :f"), {"f": fid})
            owner.execute(text("DELETE FROM firm WHERE id = :f"), {"f": fid})
        owner.commit()
        owner.close()


def _count(conn, firm_id: uuid.UUID) -> int:
    return conn.execute(
        text("SELECT count(*) FROM erpsync_entry WHERE firm_id = :f"), {"f": firm_id}
    ).scalar_one()


def test_rls_isolates_erpsync_entry_across_firms(two_firms_staged, app_engine):
    """As the unprivileged role: no context denies every staged row; firm A context
    shows only firm A's staged row, never firm B's."""
    a, b = two_firms_staged["a"], two_firms_staged["b"]
    with app_engine.connect() as conn:
        # 1) No tenant context → default-deny: neither firm's row is visible.
        assert _count(conn, a["firm"]) == 0, "firm A staged row leaked with no context"
        assert _count(conn, b["firm"]) == 0, "firm B staged row leaked with no context"

        # 2) Context = firm A → only firm A's staged row is visible.
        conn.execute(
            text("SELECT set_config('app.current_firm', :v, false)"),
            {"v": str(a["firm"])},
        )
        conn.execute(
            text("SELECT set_config('app.allowed_clients', :v, false)"),
            {"v": str(a["client"])},
        )
        assert _count(conn, a["firm"]) == 1, "firm A cannot see its own staged row"
        assert _count(conn, b["firm"]) == 0, "firm B staged row visible under firm A context"


def test_empty_firm_context_denies_erpsync_entry_rather_than_errors(app_engine):
    """0004 reuses 0003's nullif-guarded firm cast: a blank ``app.current_firm``
    resolves to NULL → zero rows (deny), never an ``invalid input syntax for type
    uuid: ""`` cast error."""
    with app_engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.current_firm', '', false)"))
        assert conn.execute(text("SELECT count(*) FROM erpsync_entry")).scalar_one() == 0
