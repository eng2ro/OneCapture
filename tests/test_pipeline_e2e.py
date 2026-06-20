"""End-to-end: a synthetic month -> staged batch, correct counts, total, hash.

Also covers the whole-batch commit policy: a malformed listing rejects the
entire batch and commits nothing.
"""

from __future__ import annotations

from decimal import Decimal

from erpsync.domain.enums import BatchStatus
from erpsync.pipeline import run_import
from gen_synthetic import malformed_rows, month_rows, write_csv, write_xlsx


def test_month_e2e_csv(tmp_path, config, store):
    listing = tmp_path / "month.csv"
    write_csv(listing, month_rows())

    result = run_import(listing, config, store)
    r = result.report

    assert result.batch_status is BatchStatus.STAGED
    assert (r.clean, r.warnings, r.duplicates, r.rejected) == (3, 3, 1, 0)
    assert result.committed_count == 6
    assert len(result.duplicate_hits) == 1

    # 1.206 + 7.020 + 11.440 + 0.576 + 0.320 (+ 0 unmapped) = 20.562
    total = sum((e.tco2e for e in result.entries), start=Decimal("0"))
    assert total == Decimal("20.562000")
    assert result.batch_hash is not None


def test_month_e2e_xlsx_matches_csv(tmp_path, config, preset, ruleset, factors, ownership):
    from erpsync.persistence.store import Store

    csv_path = tmp_path / "m.csv"
    xlsx_path = tmp_path / "m.xlsx"
    write_csv(csv_path, month_rows())
    write_xlsx(xlsx_path, month_rows())

    csv_res = run_import(csv_path, config, Store(tmp_path / "a.json"))
    xlsx_res = run_import(xlsx_path, config, Store(tmp_path / "b.json"))

    # Same data via either format -> identical batch hash.
    assert csv_res.batch_hash == xlsx_res.batch_hash


def test_malformed_listing_rejects_whole_batch(tmp_path, config, store):
    listing = tmp_path / "bad.csv"
    write_csv(listing, malformed_rows())

    result = run_import(listing, config, store)
    assert result.batch_status is BatchStatus.REJECTED
    assert result.report.rejected == 1
    assert not result.report.committable
    assert result.committed_count == 0
    assert len(store) == 0  # nothing persisted
    assert result.batch_hash is None
