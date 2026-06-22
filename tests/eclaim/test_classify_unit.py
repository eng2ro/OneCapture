"""Carbon classification unit tests (no DB) — the category-gated paths.

The factor to apply is supplied by the caller (resolved from the claim's category),
so these pass ``factor_key`` / ``unmapped`` directly and use a fake factor lookup —
no Postgres, no category table.
"""

from __future__ import annotations

from decimal import Decimal

from eclaim.ocr.base import Extraction
from eclaim.services.classify import (
    DQ_ACTIVITY,
    DQ_CATEGORY_FACTOR_MISSING,
    DQ_SPEND,
    DQ_UNMAPPED,
    SPEND_DEFAULT_SCOPE,
    SPEND_FACTOR_KEY,
    FactorView,
    classify,
)

SPEND = Decimal("0.35")

_FACTORS = {
    "fuel_diesel": FactorView("fuel_diesel", 1, 1, "L", Decimal("2.68000")),
    "electricity": FactorView("electricity", 1, 2, "kWh", Decimal("0.58500")),
}


class _Lookup:
    def get_active(self, factor_key):
        return _FACTORS.get(factor_key)


def test_category_factor_activity():
    e = Extraction(expense_type="fuel_diesel", quantity=Decimal("450"), unit="L",
                   total_amount=Decimal("2000"))
    c = classify(e, _Lookup(), SPEND, factor_key="fuel_diesel")
    assert c.basis == "activity"
    assert c.scope == 1
    assert c.factor_key == "fuel_diesel"
    assert c.tco2e == Decimal("1.206000")
    assert c.data_quality == DQ_ACTIVITY


def test_category_factor_spend_matched_when_no_quantity():
    e = Extraction(expense_type="fuel_diesel", quantity=None, total_amount=Decimal("500"))
    c = classify(e, _Lookup(), SPEND, factor_key="fuel_diesel")
    assert c.basis == "spend"
    assert c.factor_key == "fuel_diesel"   # keeps the matched factor identity
    assert c.scope == 1
    assert c.tco2e == Decimal("0.175000")  # 500 * 0.35 / 1000
    assert c.data_quality == DQ_SPEND


def test_null_factor_key_is_governed_spend_scope3():
    # classify takes factor_key directly and ignores expense_type, so any valid
    # OCR literal works here.
    e = Extraction(expense_type="other", total_amount=Decimal("1000"))
    c = classify(e, _Lookup(), SPEND, factor_key=None)
    assert c.basis == "spend"
    assert c.factor_key == SPEND_FACTOR_KEY
    assert c.scope == SPEND_DEFAULT_SCOPE
    assert c.tco2e == Decimal("0.350000")
    assert c.data_quality == DQ_SPEND


def test_unmapped_is_distinct_and_retains_quantity():
    e = Extraction(expense_type="other", quantity=Decimal("450"), unit="L",
                   total_amount=Decimal("1000"))
    c = classify(e, _Lookup(), SPEND, factor_key=None, unmapped=True)
    assert c.basis == "spend"
    assert c.factor_key == SPEND_FACTOR_KEY
    assert c.scope == SPEND_DEFAULT_SCOPE
    assert c.data_quality == DQ_UNMAPPED          # distinct, reviewable
    # Raw qty/unit retained so a later category assignment can reclassify.
    assert c.quantity == Decimal("450") and c.unit == "L"


def test_misconfigured_category_factor_is_flagged():
    e = Extraction(expense_type="other", quantity=Decimal("10"), unit="L",
                   total_amount=Decimal("1000"))
    c = classify(e, _Lookup(), SPEND, factor_key="no_such_factor")
    assert c.basis == "spend"
    assert c.factor_key == SPEND_FACTOR_KEY
    assert c.scope == SPEND_DEFAULT_SCOPE
    assert c.data_quality == DQ_CATEGORY_FACTOR_MISSING
    assert c.quantity == Decimal("10") and c.unit == "L"  # retained for review
