"""Emission factor registry.

Holds two kinds of factors, both versioned together as one factor set:

* **activity factors** — kgCO2e per physical unit (per L, per kWh, per kg).
  Used when an invoice line carries a real quantity + UoM.
* **spend (EEIO) factors** — kgCO2e per unit of currency. The fallback when no
  usable line quantity exists; entries valued this way are always DQ-flagged.

The whole set carries a ``version`` (e.g. "MY-2026.1") which is stamped on
every emission entry for replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from ..domain.enums import QuantityBasis


@dataclass(frozen=True)
class Factor:
    ref: str
    value: Decimal           # kgCO2e per unit (per `uom` for activity, per currency for spend)
    basis: QuantityBasis     # ACTIVITY or SPEND
    uom: str | None          # expected UoM for activity factors (L/kWh/KG); None for spend
    source: str = ""         # provenance, e.g. "DEFRA 2026", "MY grid", "EEIO"


@dataclass(frozen=True)
class FactorSet:
    version: str
    factors: dict[str, Factor]

    def get(self, ref: str) -> Factor:
        try:
            return self.factors[ref]
        except KeyError:
            raise KeyError(f"Unknown factor ref {ref!r} in factor set {self.version}") from None

    def __contains__(self, ref: str) -> bool:
        return ref in self.factors


def load_factor_set(path: str | Path) -> FactorSet:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    factors: dict[str, Factor] = {}
    for raw in data.get("factors", []):
        ref = str(raw["ref"])
        if ref in factors:
            raise ValueError(f"Duplicate factor ref {ref!r}")
        basis = QuantityBasis(raw["basis"])
        uom = raw.get("uom")
        if basis is QuantityBasis.ACTIVITY and not uom:
            raise ValueError(f"Activity factor {ref!r} must declare a uom")
        factors[ref] = Factor(
            ref=ref,
            value=Decimal(str(raw["value"])),
            basis=basis,
            uom=uom,
            source=str(raw.get("source", "")),
        )
    return FactorSet(version=str(data["version"]), factors=factors)
