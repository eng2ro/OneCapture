"""Shared carbon arithmetic — exact Decimal tCO2e (never float).

Factors are kgCO2e per unit; results are tonnes, so every path divides by 1000
with Decimal and quantises to 6 dp (milligram-tonne) using ``ROUND_HALF_UP`` so
the release hash is stable across runs and across modules.

    tco2e(quantity, factor)  -> activity basis (kgCO2e per physical unit)
    tco2e(amount,   factor)  -> spend basis    (kgCO2e per unit of currency)

ERP Sync's calculator and e-Claim's classifier both call :func:`tco2e`, so the
arithmetic lives in exactly one place.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_TONNE = Decimal(1000)
_QUANT = Decimal("0.000001")  # 6 dp


def quantise_tco2e(value: Decimal) -> Decimal:
    """Quantise a tonnes value to 6 dp, ROUND_HALF_UP."""
    return value.quantize(_QUANT, rounding=ROUND_HALF_UP)


def tco2e(units: Decimal, factor_value: Decimal) -> Decimal:
    """kgCO2e/unit × units → tonnes CO2e, quantised to 6 dp.

    ``units`` is the activity quantity (activity basis) or the spend amount
    (spend basis); ``factor_value`` is the matching factor.
    """
    return quantise_tco2e(units * factor_value / _TONNE)
