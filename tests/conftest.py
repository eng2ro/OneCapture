"""Shared fixtures: load the ABC Manufacturing config with an isolated store."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"

# Make scripts/gen_synthetic.py importable as a module.
sys.path.insert(0, str(ROOT / "scripts"))

from erpsync.dedup.crosschannel import OwnershipMatrix  # noqa: E402
from erpsync.emissions.factors import load_factor_set  # noqa: E402
from erpsync.ingest.column_preset import ColumnPreset  # noqa: E402
from erpsync.persistence.store import Store  # noqa: E402
from erpsync.pipeline import PipelineConfig  # noqa: E402
from erpsync.rules.ruleset import load_ruleset  # noqa: E402


@pytest.fixture
def factors():
    return load_factor_set(CONFIG / "factors" / "my_2026.yaml")


@pytest.fixture
def ruleset():
    return load_ruleset(CONFIG / "rules" / "abc_manufacturing.v7.yaml")


@pytest.fixture
def preset():
    return ColumnPreset.from_yaml(CONFIG / "presets" / "abc_manufacturing.yaml")


@pytest.fixture
def ownership():
    return OwnershipMatrix.from_yaml(CONFIG / "ownership" / "abc_manufacturing.yaml")


@pytest.fixture
def config(preset, ruleset, factors, ownership) -> PipelineConfig:
    return PipelineConfig(
        client_id="abc_manufacturing",
        preset=preset,
        ruleset=ruleset,
        factors=factors,
        ownership=ownership,
        eclaim_doc_numbers={"AP-2026-042"},
    )


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "store.json")
