"""Shared core: tCO2e arithmetic, canonical hashing, audit chain.

DB-independent — runs anywhere. Guards the logic ERP Sync and e-Claim share.
"""

from __future__ import annotations

from decimal import Decimal

from core.audit import chain_hash, verify_chain
from core.carbon import tco2e
from core.release import canonical_hash


def test_tco2e_matches_known_values():
    # 450 L * 2.68 / 1000 = 1.206 t
    assert tco2e(Decimal("450"), Decimal("2.68")) == Decimal("1.206000")
    # spend: 3200 * 0.18 / 1000 = 0.576 t
    assert tco2e(Decimal("3200"), Decimal("0.18")) == Decimal("0.576000")


def test_canonical_hash_is_order_independent():
    a = [{"k": "1", "v": "x"}, {"k": "2", "v": "y"}]
    assert canonical_hash(a) == canonical_hash(list(reversed(a)))


def test_canonical_hash_changes_with_content():
    base = [{"k": "1", "tco2e": "1.000000"}]
    changed = [{"k": "1", "tco2e": "2.000000"}]
    assert canonical_hash(base) != canonical_hash(changed)


def test_audit_chain_links_and_verifies():
    p1 = {"event_type": "submitted"}
    p2 = {"event_type": "approved"}
    p3 = {"event_type": "released"}
    h1 = chain_hash(None, p1)
    h2 = chain_hash(h1, p2)
    h3 = chain_hash(h2, p3)
    assert verify_chain([(None, p1, h1), (h1, p2, h2), (h2, p3, h3)])


def test_audit_chain_detects_tampering():
    p1 = {"event_type": "submitted"}
    p2 = {"event_type": "approved"}
    h1 = chain_hash(None, p1)
    h2 = chain_hash(h1, p2)
    tampered = {"event_type": "rejected"}  # someone rewrote event 1's payload
    assert not verify_chain([(None, tampered, h1), (h1, p2, h2)])
