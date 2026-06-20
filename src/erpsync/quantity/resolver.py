"""Quantity resolution (FR-S4).

Given a source line, the rule that matched it, and the active factor set,
decide *how* the line should be valued:

* **activity** — the line has a usable quantity whose UoM matches the rule's
  activity factor. Highest data quality (``MEASURED``).
* **spend** — no usable line quantity, so we fall back to the rule's EEIO
  factor applied to the line amount. Always ``ESTIMATED`` and surfaced as a
  warning.
* **unvaluable** — mappable but neither path is possible (e.g. activity rule
  with no quantity *and* no spend fallback configured). ``FLAGGED`` for the
  review queue; valued at zero so it never silently inflates totals.

This stage only chooses the basis and the factor; the arithmetic lives in
emissions/calculator.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.enums import DataQuality, QuantityBasis
from ..domain.models import MappingRule, SourceRecord
from ..emissions.factors import FactorSet


@dataclass(frozen=True)
class Resolution:
    basis: QuantityBasis
    data_quality: DataQuality
    quantity: Decimal | None        # activity quantity (None for spend / unvaluable)
    uom: str | None
    amount: Decimal | None          # spend amount used (None for activity)
    factor_ref: str | None          # None when unvaluable
    notes: tuple[str, ...] = ()


def _uom_match(record_uom: str | None, factor_uom: str | None) -> bool:
    if not record_uom or not factor_uom:
        return False
    return record_uom.strip().upper() == factor_uom.strip().upper()


def _has_quantity(record: SourceRecord) -> bool:
    return record.quantity is not None and record.quantity > 0


def resolve(record: SourceRecord, rule: MappingRule, factors: FactorSet) -> Resolution:
    """Resolve the valuation basis for one mapped line."""
    factor = factors.get(rule.factor_ref)

    # A rule whose primary basis is SPEND values directly off the EEIO factor.
    if rule.basis is QuantityBasis.SPEND:
        return Resolution(
            basis=QuantityBasis.SPEND,
            data_quality=DataQuality.ESTIMATED,
            quantity=None,
            uom=None,
            amount=record.amount,
            factor_ref=rule.factor_ref,
            notes=("spend-based by rule",),
        )

    # ACTIVITY rule: prefer the real line quantity when present and UoM-aligned.
    if _has_quantity(record) and _uom_match(record.uom, factor.uom):
        return Resolution(
            basis=QuantityBasis.ACTIVITY,
            data_quality=DataQuality.MEASURED,
            quantity=record.quantity,
            uom=record.uom,
            amount=None,
            factor_ref=rule.factor_ref,
        )

    # Activity not usable — record why, then try the spend fallback.
    if _has_quantity(record) and not _uom_match(record.uom, factor.uom):
        why = f"line UoM {record.uom!r} != factor UoM {factor.uom!r}; fell back to spend"
    else:
        why = "no usable line quantity; fell back to spend"

    if rule.spend_fallback_ref and record.amount is not None:
        return Resolution(
            basis=QuantityBasis.SPEND,
            data_quality=DataQuality.ESTIMATED,
            quantity=None,
            uom=None,
            amount=record.amount,
            factor_ref=rule.spend_fallback_ref,
            notes=(why,),
        )

    # Mappable but unvaluable — keep it, flag it, value it at zero.
    return Resolution(
        basis=QuantityBasis.SPEND,
        data_quality=DataQuality.FLAGGED,
        quantity=None,
        uom=None,
        amount=record.amount,
        factor_ref=None,
        notes=(why, "no spend fallback configured — needs review"),
    )
