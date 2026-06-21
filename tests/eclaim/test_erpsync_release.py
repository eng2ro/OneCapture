"""ERP Sync release: clean ``erpsync_entry`` rows → the shared immutable ledger.

Exercises the standalone, on-demand release step against the real Postgres test
DB (skips cleanly when none is reachable). Each test first stages the synthetic
ABC Manufacturing month through the pipeline → ``erpsync_entry`` (3 clean,
3 flagged, 1 held), then releases the clean rows via ``release_clean`` and checks:

* **projection** — one ``release_batch`` + one ``emission_entry`` per clean row,
  with the right ``source_type`` / scope-int / tCO2e, and ONLY clean rows released;
* **status flip** — those rows go ``clean → released``; held/flagged are untouched;
* **idempotency** — a second release is a no-op (no batch, no new ledger rows);
* **audit** — a hash-chained ``released`` event per row + a batch ``tsa_anchored``;
* **RLS** — a released ERP Sync ledger row is invisible under a foreign firm
  context, on the unprivileged ``onecapture_app`` connection.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select, text

from core.audit import verify_chain
from eclaim.db.models import AuditEvent, EmissionEntry, ErpsyncEntry, ReleaseBatch
from eclaim.repositories import AuditRepository
from erpsync.persistence.pg_staging import PgStagingStore
from erpsync.persistence.store import Store
from erpsync.pipeline import run_import
from erpsync.release.service import release_clean
from gen_synthetic import month_rows, write_csv


def _stage_month(db_session, config, tmp_path) -> dict:
    """Stage the synthetic month into erpsync_entry for the seeded firm/client,
    returning the tenant ids."""
    ids = db_session.info["principal"]
    sink = PgStagingStore(db_session, firm_id=ids["firm"], client_id=ids["client"])
    listing = tmp_path / "month.csv"
    write_csv(listing, month_rows())
    run_import(listing, config, Store(), staging=sink)
    db_session.flush()
    return ids


def _payload(event: AuditEvent) -> dict:
    """The exact payload ``record_event`` hashed, rebuilt from a stored event, so
    ``verify_chain`` recomputes the same digest (the hash is over the whole event,
    not just ``detail``)."""
    return {
        "entity_type": event.entity_type,
        "entity_id": str(event.entity_id),
        "event_type": event.event_type,
        "actor": event.actor,
        "detail": event.detail or {},
    }


def _status_counts(db_session, client_id) -> dict[str, int]:
    rows = db_session.execute(
        text(
            "SELECT status, count(*) FROM erpsync_entry "
            "WHERE client_id = :c GROUP BY status"
        ),
        {"c": client_id},
    ).all()
    return {status: n for status, n in rows}


# --------------------------------------------------------------------------- #
# Projection correctness + status flip
# --------------------------------------------------------------------------- #
def test_release_projects_only_clean_rows_into_the_ledger(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)

    # Snapshot the clean rows before release (id → (scope, tco2e)).
    clean = {
        r.id: (r.scope, r.tco2e)
        for r in db_session.execute(
            select(ErpsyncEntry).where(
                ErpsyncEntry.client_id == ids["client"],
                ErpsyncEntry.status == "clean",
            )
        ).scalars()
    }
    assert len(clean) == 3  # the synthetic month stages 3 clean rows
    expected_total = sum((tco2e for _, tco2e in clean.values()), start=Decimal("0"))

    batch = release_clean(
        db_session, firm_id=ids["firm"], client_id=ids["client"], actor="releaser"
    )

    # One batch, stamped erpsync, covering exactly the clean rows.
    assert batch is not None
    assert batch.source_type == "erpsync"
    assert batch.record_count == 3
    assert batch.total_tco2e == expected_total
    assert batch.tsa_token  # stub anchor recorded

    # One ledger entry per clean row, projected correctly; nothing else released.
    entries = list(
        db_session.execute(
            select(EmissionEntry).where(EmissionEntry.release_batch_id == batch.id)
        ).scalars()
    )
    assert len(entries) == 3
    scope_int = {"scope_1": 1, "scope_2": 2}
    for e in entries:
        assert e.source_type == "erpsync"
        assert e.source_id in clean
        src_scope, src_tco2e = clean[e.source_id]
        assert e.scope == scope_int.get(src_scope, 3)
        assert e.tco2e == src_tco2e
    assert sum((e.tco2e for e in entries), start=Decimal("0")) == expected_total


def test_release_flips_clean_to_released_and_leaves_review_rows(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    assert _status_counts(db_session, ids["client"]) == {"clean": 3, "flagged": 3, "held": 1}

    release_clean(db_session, firm_id=ids["firm"], client_id=ids["client"], actor="releaser")

    # clean → released; held/flagged stay for review (FR-S5).
    assert _status_counts(db_session, ids["client"]) == {
        "released": 3,
        "flagged": 3,
        "held": 1,
    }


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_re_release_is_a_noop(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)

    first = release_clean(db_session, firm_id=ids["firm"], client_id=ids["client"], actor="r")
    assert first is not None

    def _counts():
        n_entries = db_session.execute(
            select(func.count())
            .select_from(EmissionEntry)
            .where(EmissionEntry.client_id == ids["client"])
        ).scalar_one()
        n_batches = db_session.execute(
            select(func.count())
            .select_from(ReleaseBatch)
            .where(ReleaseBatch.client_id == ids["client"])
        ).scalar_one()
        return n_entries, n_batches

    after_first = _counts()
    assert after_first == (3, 1)

    # Nothing is clean any more → no batch, no new ledger rows.
    second = release_clean(db_session, firm_id=ids["firm"], client_id=ids["client"], actor="r")
    assert second is None
    assert _counts() == after_first


# --------------------------------------------------------------------------- #
# Audit chain
# --------------------------------------------------------------------------- #
def test_release_writes_a_verifiable_audit_trail(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    released_ids = [
        r.id
        for r in db_session.execute(
            select(ErpsyncEntry).where(
                ErpsyncEntry.client_id == ids["client"],
                ErpsyncEntry.status == "clean",
            )
        ).scalars()
    ]

    batch = release_clean(
        db_session, firm_id=ids["firm"], client_id=ids["client"], actor="releaser"
    )
    audit = AuditRepository(db_session)

    # One 'released' event per row, each a valid (single-link) chain.
    for entry_id in released_ids:
        chain = audit.chain("erpsync_entry", entry_id)
        assert [e.event_type for e in chain] == ["released"]
        assert verify_chain([(e.prev_hash, _payload(e), e.hash) for e in chain])

    # The batch is anchored once.
    batch_chain = audit.chain("release_batch", batch.id)
    assert [e.event_type for e in batch_chain] == ["tsa_anchored"]
    assert batch_chain[0].actor == "system"
    assert verify_chain([(e.prev_hash, _payload(e), e.hash) for e in batch_chain])


# --------------------------------------------------------------------------- #
# RLS isolation of released rows
# --------------------------------------------------------------------------- #
def test_released_rows_are_rls_isolated_by_firm(db_session, config, tmp_path):
    """A released ERP Sync ledger row is visible under its own firm context but
    invisible under a foreign firm's — RLS bites on the unprivileged role. Done
    by switching the GUC context on the same app-role connection (the rows are
    uncommitted, so a second connection couldn't see them at all)."""
    ids = _stage_month(db_session, config, tmp_path)
    release_clean(db_session, firm_id=ids["firm"], client_id=ids["client"], actor="r")

    def _visible_erpsync_entries() -> int:
        return db_session.execute(
            text(
                "SELECT count(*) FROM emission_entry "
                "WHERE source_type = 'erpsync' AND firm_id = :f"
            ),
            {"f": ids["firm"]},
        ).scalar_one()

    # Own firm context (set by the db_session fixture): the 3 released rows show.
    assert _visible_erpsync_entries() == 3

    # Switch to a foreign firm with no grants → RLS hides firm A's released rows.
    foreign_firm = uuid.uuid4()
    db_session.execute(
        text("SELECT set_config('app.current_firm', :v, true)"),
        {"v": str(foreign_firm)},
    )
    db_session.execute(
        text("SELECT set_config('app.allowed_clients', '', true)")
    )
    assert _visible_erpsync_entries() == 0

    # Restore firm A context so the rest of the transaction/teardown is unaffected.
    db_session.execute(
        text("SELECT set_config('app.current_firm', :v, true)"),
        {"v": str(ids["firm"])},
    )
    db_session.execute(
        text("SELECT set_config('app.allowed_clients', :v, true)"),
        {"v": str(ids["client"])},
    )
    assert _visible_erpsync_entries() == 3
