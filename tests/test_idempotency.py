"""FR-S1: idempotent re-import by line key, surviving across store instances."""

from __future__ import annotations

from erpsync.domain.enums import BatchStatus
from erpsync.persistence.store import Store
from erpsync.pipeline import run_import
from gen_synthetic import month_rows, write_csv


def _listing(tmp_path):
    p = tmp_path / "month.csv"
    write_csv(p, month_rows())
    return p


def test_second_import_commits_nothing(tmp_path, config, store):
    listing = _listing(tmp_path)

    first = run_import(listing, config, store)
    assert first.batch_status is BatchStatus.STAGED
    assert first.committed_count == 6  # 7 rows - 1 cross-channel dup held back

    second = run_import(listing, config, store)
    assert second.committed_count == 0
    assert second.report.duplicates == 7  # 6 idempotency + 1 cross-channel


def test_idempotency_survives_new_store_instance(tmp_path, config):
    listing = _listing(tmp_path)
    store_path = tmp_path / "persist.json"

    run_import(listing, config, Store(store_path))
    # Fresh process simulation: brand-new Store reading the same file.
    reloaded = Store(store_path)
    result = run_import(listing, config, reloaded)
    assert result.committed_count == 0


def test_batch_hash_stable_across_runs(tmp_path, config):
    listing = _listing(tmp_path)
    h1 = run_import(listing, config, Store(tmp_path / "a.json")).batch_hash
    h2 = run_import(listing, config, Store(tmp_path / "b.json")).batch_hash
    assert h1 == h2 and h1 is not None
