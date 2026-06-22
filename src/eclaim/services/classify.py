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

# Data-quality labels (carried onto the claim and surfaced to review). The two
# "Unmapped" labels share that prefix so a review filter can catch both with a
# single ``data_quality LIKE 'Unmapped%'``.
DQ_ACTIVITY = "Activity-based"
DQ_SPEND = "Spend-based — lower data quality"
DQ_UNMAPPED = "Unmapped — no category (needs review)"
DQ_CATEGORY_FACTOR_MISSING = "Unmapped — category factor inactive (needs review)"

# Synthetic factor identity for pure spend (no usable factor). Satisfies the
# NOT NULL factor columns on emission_entry without inventing a factor row.
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
    extraction: Extraction,
    factors: FactorLookup,
    spend_factor: Decimal,
    *,
    factor_key: str | None,
    unmapped: bool = False,
) -> Classification:
    """Classify one extracted receipt into a carbon result.

    The factor to apply is supplied by the caller (resolved from the claim's
    category), not derived from ``expense_type`` here:

    * ``unmapped`` — no category matched. A valid spend_eeio/scope-3 row is still
      produced (so the claim is releasable), but flagged ``DQ_UNMAPPED`` and the
      OCR quantity/unit are *retained* so a reviewer who later assigns a category
      can reclassify to activity. Never silently absorbed.
    * ``factor_key is None`` — a category that is spend-based by intent: governed
      spend at scope 3, ``DQ_SPEND``.
    * ``factor_key`` set but no active EF — a misconfigured category: spend_eeio at
      scope 3, flagged ``DQ_CATEGORY_FACTOR_MISSING`` for review.
    * ``factor_key`` set with an active EF — the existing activity (usable
      quantity) / spend-matched (no quantity) paths, scope + version factor-derived.
    """
    qty = extraction.quantity
    amount = extraction.total_amount or Decimal("0")

    if unmapped:
        # No category — defer the decision: keep the raw qty/unit for reassignment.
        return _spend_default(
            amount, spend_factor, DQ_UNMAPPED, quantity=qty, unit=extraction.unit
        )

    if factor_key is None:
        return _spend_default(amount, spend_factor, DQ_SPEND)  # governed spend

    factor = factors.get_active(factor_key)
    if factor is None:
        # Category names a factor with no active EF — flag it, keep raw qty/unit.
        return _spend_default(
            amount, spend_factor, DQ_CATEGORY_FACTOR_MISSING,
            quantity=qty, unit=extraction.unit,
        )

    # Activity-based: real factor + usable quantity.
    if qty is not None and qty > 0:
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

    # Spend-matched: factor known but no usable quantity — keep its identity/scope.
    return Classification(
        scope=factor.scope,
        factor_key=factor.factor_key,
        factor_version=factor.version,
        basis="spend",
        tco2e=_tco2e(amount, spend_factor),
        data_quality=DQ_SPEND,
        quantity=None,
        unit=None,
    )


def _spend_default(
    amount: Decimal,
    spend_factor: Decimal,
    data_quality: str,
    *,
    quantity: Decimal | None = None,
    unit: str | None = None,
) -> Classification:
    """The generic spend_eeio / scope-3 result, with a caller-chosen
    ``data_quality``. ``quantity``/``unit`` are retained only for the review
    states (unmapped / misconfigured), so a later reclassification can use them."""
    return Classification(
        scope=SPEND_DEFAULT_SCOPE,
        factor_key=SPEND_FACTOR_KEY,
        factor_version=SPEND_FACTOR_VERSION,
        basis="spend",
        tco2e=_tco2e(amount, spend_factor),
        data_quality=data_quality,
        quantity=quantity,
        unit=unit,
    )
