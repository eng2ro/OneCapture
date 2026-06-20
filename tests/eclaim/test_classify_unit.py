"""Carbon classification unit tests (no DB) — spec §4 paths.

Uses a fake factor lookup so the activity / spend-fallback branches are exercised
without Postgres.
"""

from __future__ import annotations

from decimal import Decimal

from eclaim.ocr.base import Extraction
from eclaim.services.classify import (
    DQ_ACTIVITY,
    DQ_SPEND,
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


def test_diesel_activity():
    e = Extraction(expense_type="fuel_diesel", quantity=Decimal("450"), unit="L",
                   total_amount=Decimal("2000"))
    c = classify(e, _Lookup(), SPEND)
    assert c.basis == "activity"
    assert c.scope == 1
    assert c.factor_key == "fuel_diesel"
    assert c.tco2e == Decimal("1.206000")
    assert c.data_quality == DQ_ACTIVITY


def test_electricity_activity():
    e = Extraction(expense_type="electricity", quantity=Decimal("12000"), unit="kWh")
    c = classify(e, _Lookup(), SPEND)
    assert c.basis == "activity"
    assert c.scope == 2
    assert c.tco2e == Decimal("7.020000")


def test_no_quantity_falls_back_to_spend():
    e = Extraction(expense_type="fuel_diesel", quantity=None, total_amount=Decimal("500"))
    c = classify(e, _Lookup(), SPEND)
    assert c.basis == "spend"
    assert c.factor_key == "fuel_diesel"   # keeps the matched factor identity
    assert c.scope == 1
    assert c.tco2e == Decimal("0.175000")  # 500 * 0.35 / 1000
    assert c.data_quality == DQ_SPEND


def test_other_expense_is_generic_spend_scope3():
    e = Extraction(expense_type="other", total_amount=Decimal("1000"))
    c = classify(e, _Lookup(), SPEND)
    assert c.basis == "spend"
    assert c.factor_key == SPEND_FACTOR_KEY
    assert c.scope == 3
    assert c.tco2e == Decimal("0.350000")
