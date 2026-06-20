"""Carbon classification (shared logic, spec §4).

Maps an OCR :class:`Extraction` to a carbon :class:`Classification` using the
versioned ``emission_factor`` library and the exact Decimal arithmetic in
:func:`core.carbon.tco2e` — the same maths ERP Sync uses.

Two paths, mirroring the ERP Sync engine:

* **activity** — a matching factor exists *and* ``quantity > 0`` →
  ``tco2e = quantity × factor_kg_per_unit / 1000``; highest data quality.
* **spend fallback** — otherwise → ``tco2e = total_amount × SPEND_FACTOR / 1000``;
  always flagged as lower data quality.

This module is storage-agnostic: it takes a :class:`FactorLookup`, so it is
unit-testable with a fake and identical whether called from the API or a test.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from core.carbon import tco2e as _tco2e

from ..ocr.base import Extraction

# Data-quality labels (spec §4 wording, carried onto the claim and surfaced to review).
DQ_ACTIVITY = "Activity-based"
DQ_SPEND = "Spend-based — lower data quality"

# Synthetic factor identity for pure spend (no expense_type match). Satisfies
# the NOT NULL factor columns on emission_entry without inventing a factor row.
SPEND_FACTOR_KEY = "spend_eeio"
SPEND_FACTOR_VERSION = 0
SPEND_DEFAULT_SCOPE = 3


@dataclass(frozen=True)
class FactorView:
    """The fields of an active ``emission_factor`` row the classifier needs."""

    factor_key: str
    version: int
    scope: int
    unit: str
    factor_kg_per_unit: Decimal


class FactorLookup(Protocol):
    """Resolves an expense type to the active emission factor, if any."""

    def get_active(self, factor_key: str) -> FactorView | None: ...


@dataclass(frozen=True)
class Classification:
    scope: int
    factor_key: str
    factor_version: int
    basis: str  # 'activity' | 'spend'
    tco2e: Decimal
    data_quality: str
    quantity: Decimal | None
    unit: str | None


def classify(
    extraction: Extraction, factors: FactorLookup, spend_factor: Decimal
) -> Classification:
    """Classify one extracted receipt into a carbon result."""
    factor = (
        None
        if extraction.expense_type == "other"
        else factors.get_active(extraction.expense_type)
    )
    qty = extraction.quantity
    amount = extraction.total_amount or Decimal("0")

    # Activity-based: real factor + usable quantity.
    if factor is not None and qty is not None and qty > 0:
        return Classification(
            scope=factor.scope,
            factor_key=factor.factor_key,
            factor_version=factor.version,
            basis="activity",
            tco2e=_tco2e(qty, factor.factor_kg_per_unit),
            data_quality=DQ_ACTIVITY,
            quantity=qty,
            unit=extraction.unit or factor.unit,
        )

    # Spend-based fallback. Keep the matched factor's identity/scope when we know
    # it, else fall back to a generic spend identity at scope 3.
    if factor is not None:
        key, version, scope = factor.factor_key, factor.version, factor.scope
    else:
        key, version, scope = SPEND_FACTOR_KEY, SPEND_FACTOR_VERSION, SPEND_DEFAULT_SCOPE

    return Classification(
        scope=scope,
        factor_key=key,
        factor_version=version,
        basis="spend",
        tco2e=_tco2e(amount, spend_factor),
        data_quality=DQ_SPEND,
        quantity=None,
        unit=None,
    )
