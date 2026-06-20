"""FR-S8: cross-channel dedup via ownership matrix + document-number match."""

from __future__ import annotations

from decimal import Decimal

from erpsync.dedup.crosschannel import screen
from erpsync.domain.enums import Channel, DataQuality, QuantityBasis, Scope
from erpsync.domain.models import EmissionEntry


def _entry(category, doc_number, key=("c", "1", 0)) -> EmissionEntry:
    return EmissionEntry(
        line_key=key, doc_entry=key[1], line_num=key[2], doc_number=doc_number,
        category=category, scope=Scope.S1, basis=QuantityBasis.ACTIVITY,
        data_quality=DataQuality.MEASURED, quantity=Decimal("1"), uom="L",
        amount=None, factor_ref="DIESEL_B7", factor_value=Decimal("2.68"),
        factor_version="v", rule_id="r", rule_version="v7",
        tco2e=Decimal("0.002680"), source_hash="x",
    )


def test_planted_duplicate_doc_in_both_channels_is_caught(ownership):
    # A fuel invoice ERP Sync owns, but its DocNum already exists in e-Claim.
    entries = [_entry("Fleet diesel", "AP-2026-042")]
    hits = screen(entries, ownership, eclaim_doc_numbers={"AP-2026-042"})
    assert len(hits) == 1
    assert hits[0].owning_channel is Channel.E_CLAIM
    assert "already captured in e-Claim" in hits[0].reason


def test_no_hit_when_doc_not_in_eclaim(ownership):
    entries = [_entry("Fleet diesel", "AP-2026-001")]
    assert screen(entries, ownership, eclaim_doc_numbers={"AP-2026-042"}) == []


def test_ownership_violation_is_caught(ownership):
    # ERP Sync produced a category e-Claim owns -> boundary violation.
    entries = [_entry("Staff fuel claim", "AP-2026-077")]
    hits = screen(entries, ownership)
    assert len(hits) == 1
    assert hits[0].owning_channel is Channel.E_CLAIM
    assert "owned by" in hits[0].reason


def test_erp_owned_category_passes(ownership):
    entries = [_entry("Electricity", "AP-2026-010")]
    assert screen(entries, ownership) == []
