"""tCO2e calculation — exact Decimal arithmetic.

Factors are expressed in kgCO2e per unit; results are reported in tonnes, so
every path divides by 1000 with Decimal (never float). The result is quantised
to 6 decimal places (milligram-tonne precision) using ``ROUND_HALF_UP`` so the
release hash is stable across runs.

    activity:  tCO2e = quantity * factor_value / 1000
    spend:     tCO2e = amount   * factor_value / 1000
    unvaluable: tCO2e = 0
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.carbon import tco2e as _tco2e

from ..domain.enums import QuantityBasis
from ..emissions.factors import Factor, FactorSet
from ..quantity.resolver import Resolution


@dataclass(frozen=True)
class EmissionResult:
    tco2e: Decimal
    factor_ref: str | None
    factor_value: Decimal
    factor_version: str


def compute(resolution: Resolution, factors: FactorSet) -> EmissionResult:
    """Compute tCO2e for a resolved line."""
    if resolution.factor_ref is None:
        # unvaluable — flagged upstream; contributes zero until reviewed
        return EmissionResult(
            tco2e=Decimal("0.000000"),
            factor_ref=None,
            factor_value=Decimal(0),
            factor_version=factors.version,
        )

    factor: Factor = factors.get(resolution.factor_ref)

    if resolution.basis is QuantityBasis.ACTIVITY:
        if resolution.quantity is None:
            raise ValueError("activity basis requires a quantity")
        units = resolution.quantity
    else:  # SPEND
        if resolution.amount is None:
            raise ValueError("spend basis requires an amount")
        units = resolution.amount

    tco2e = _tco2e(units, factor.value)
    return EmissionResult(
        tco2e=tco2e,
        factor_ref=factor.ref,
        factor_value=factor.value,
        factor_version=factors.version,
    )
