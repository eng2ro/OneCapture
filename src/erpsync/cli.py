"""CLI: import an AP listing and print the validation report + batch hash.

    erpsync-import --config config/clients/abc_manufacturing.yaml listing.csv

The config file names the per-client preset / ruleset / factor set / ownership
matrix and the persistence store path, so the operator only supplies a listing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .dedup.crosschannel import OwnershipMatrix
from .domain.enums import BatchStatus
from .emissions.factors import load_factor_set
from .ingest.column_preset import ColumnPreset
from .persistence.store import Store
from .pipeline import PipelineConfig, PipelineResult, run_import
from .rules.ruleset import load_ruleset


def _load_config(config_path: Path) -> tuple[PipelineConfig, Store]:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    base = config_path.parent

    def _p(rel: str) -> Path:
        return (base / rel).resolve()

    client_id = str(data["client_id"])
    preset = (
        ColumnPreset.from_yaml(_p(data["preset"]))
        if data.get("preset")
        else ColumnPreset.default(client_id)
    )
    ruleset = load_ruleset(_p(data["ruleset"]))
    factors = load_factor_set(_p(data["factors"]))
    ownership = OwnershipMatrix.from_yaml(_p(data["ownership"]))
    eclaim = set(data.get("eclaim_doc_numbers", []))

    store_path = _p(data["store"]) if data.get("store") else None
    store = Store(store_path)

    config = PipelineConfig(
        client_id=client_id,
        preset=preset,
        ruleset=ruleset,
        factors=factors,
        ownership=ownership,
        eclaim_doc_numbers=eclaim,
    )
    return config, store


def _print_report(result: PipelineResult) -> None:
    r = result.report
    print(f"\nImport validation report - {r.file_name}")
    print(f"  file SHA-256 : {r.file_sha256}")
    print(f"  total rows   : {r.total_rows}")
    print(
        f"  clean={r.clean}  warning={r.warnings}  "
        f"duplicate={r.duplicates}  rejected={r.rejected}"
    )
    print(f"  committable  : {r.committable}")

    if r.rejected:
        print("\n  Rejected rows (block the whole-batch commit):")
        for o in r.outcomes:
            if o.status.value == "rejected":
                print(f"    row {o.row_index}: {'; '.join(o.messages)}")

    if result.duplicate_hits:
        print("\n  Cross-channel duplicates held back (FR-S8):")
        for h in result.duplicate_hits:
            print(f"    {h.doc_number or h.line_key}: {h.reason}")

    print(f"\n  Batch status : {result.batch_status.value}")
    print(f"  Committed    : {result.committed_count} new entry(ies)")
    if result.batch_hash:
        print(f"  Batch hash   : {result.batch_hash}")
        total = sum((e.tco2e for e in result.entries), start=_dec0())
        print(f"  Total tCO2e  : {total}")
    print()


def _dec0():
    from decimal import Decimal

    return Decimal("0")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="erpsync-import", description=__doc__)
    parser.add_argument("listing", help="path to the AP listing (.csv/.xlsx)")
    parser.add_argument(
        "--config", required=True, help="path to the client config YAML"
    )
    args = parser.parse_args(argv)

    config, store = _load_config(Path(args.config))
    result = run_import(args.listing, config, store)
    _print_report(result)

    # Non-zero exit when the batch could not be staged, for scripting.
    return 0 if result.batch_status is not BatchStatus.REJECTED else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
