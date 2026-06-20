"""FR-S6: deterministic, order-independent batch hash + stubbed release seam."""

from __future__ import annotations

from decimal import Decimal

from erpsync.domain.enums import BatchStatus, DataQuality, QuantityBasis, Scope
from erpsync.domain.models import EmissionEntry
from erpsync.release.gate import ReleaseGate, batch_hash


def _entry(key, tco2e="1.000000") -> EmissionEntry:
    return EmissionEntry(
        line_key=key, doc_entry=key[1], line_num=key[2], doc_number=f"D-{key[1]}",
        category="Fleet diesel", scope=Scope.S1, basis=QuantityBasis.ACTIVITY,
        data_quality=DataQuality.MEASURED, quantity=Decimal("1"), uom="L",
        amount=None, factor_ref="DIESEL_B7", factor_value=Decimal("2.68"),
        factor_version="MY-2026.1", rule_id="r", rule_version="v7",
        tco2e=Decimal(tco2e), source_hash="abc",
    )


def test_hash_is_deterministic():
    e = [_entry(("c", "1", 0)), _entry(("c", "2", 0))]
    assert batch_hash(e) == batch_hash(e)


def test_hash_is_order_independent():
    a = [_entry(("c", "1", 0)), _entry(("c", "2", 0))]
    b = list(reversed(a))
    assert batch_hash(a) == batch_hash(b)


def test_hash_changes_when_content_changes():
    base = [_entry(("c", "1", 0), tco2e="1.000000")]
    changed = [_entry(("c", "1", 0), tco2e="2.000000")]
    assert batch_hash(base) != batch_hash(changed)


def test_release_uses_stub_seams_and_anchors_hash():
    entries = [_entry(("c", "1", 0))]
    gate = ReleaseGate()
    result = gate.release(entries)
    assert result.status is BatchStatus.RELEASED
    assert result.batch_hash == batch_hash(entries)
    # Pass-1 seams are clearly marked as stubs, not real services.
    assert result.tsa_token.startswith("STUB-TSA:")
    assert result.sink_receipt.startswith("STUB-CARBONNEXT:")
