"""Exact tCO2e arithmetic (Decimal, no float drift)."""

from __future__ import annotations

from decimal import Decimal

from erpsync.domain.enums import DataQuality, QuantityBasis
from erpsync.emissions.calculator import compute
from erpsync.quantity.resolver import Resolution


def _activity(qty, factor_ref, uom):
    return Resolution(
        basis=QuantityBasis.ACTIVITY, data_quality=DataQuality.MEASURED,
        quantity=Decimal(qty), uom=uom, amount=None, factor_ref=factor_ref,
    )


def _spend(amount, factor_ref):
    return Resolution(
        basis=QuantityBasis.SPEND, data_quality=DataQuality.ESTIMATED,
        quantity=None, uom=None, amount=Decimal(amount), factor_ref=factor_ref,
    )


def test_diesel_activity_exact(factors):
    # 450 L * 2.68 kg/L = 1206 kg = 1.206 t
    r = compute(_activity("450", "DIESEL_B7", "L"), factors)
    assert r.tco2e == Decimal("1.206000")
    assert r.factor_value == Decimal("2.68")
    assert r.factor_version == "MY-2026.1"


def test_electricity_activity_exact(factors):
    # 12000 kWh * 0.585 = 7020 kg = 7.020 t
    assert compute(_activity("12000", "GRID_MY", "kWh"), factors).tco2e == Decimal("7.020000")


def test_refrigerant_activity_exact(factors):
    # 8 kg * 1430 = 11440 kg = 11.44 t
    assert compute(_activity("8", "REFRIGERANT_R134A", "KG"), factors).tco2e == Decimal("11.440000")


def test_freight_spend_exact(factors):
    # 3200 MYR * 0.18 = 576 kg = 0.576 t
    assert compute(_spend("3200", "EEIO_FREIGHT"), factors).tco2e == Decimal("0.576000")


def test_no_float_drift_on_awkward_value(factors):
    # 0.1 + 0.2 style trap: 333.33 L * 2.68 = 893.3244 kg = 0.8933244 -> 6dp round
    r = compute(_activity("333.33", "DIESEL_B7", "L"), factors)
    assert r.tco2e == Decimal("0.893324")  # 0.8933244 -> half-up at 6dp


def test_unvaluable_is_zero(factors):
    res = Resolution(
        basis=QuantityBasis.SPEND, data_quality=DataQuality.FLAGGED,
        quantity=None, uom=None, amount=Decimal("100"), factor_ref=None,
    )
    assert compute(res, factors).tco2e == Decimal("0.000000")
