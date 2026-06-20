"""Immutable domain models.

All monetary and quantity fields are ``Decimal`` — never float. Financial and
tCO2e figures here end up in an audited evidence pack, so we keep exact
decimal arithmetic end to end (see emissions/calculator.py). pydantic is
configured to *not* coerce floats into these fields silently.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    Channel,
    DataQuality,
    QuantityBasis,
    RowStatus,
    Scope,
)


class _Frozen(BaseModel):
    """Base: frozen, strict-ish. Rejects float→Decimal coercion."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #
class SourceRecord(_Frozen):
    """One invoice *line* read from an AP listing, normalised.

    The identity tuple is ``(client_id, doc_entry, line_num)``. ``doc_entry``
    is the ERP document key (SAP B1 DocEntry); ``line_num`` distinguishes
    lines within a multi-line invoice. ``raw`` retains the untouched cell map
    for the evidence pack.
    """

    client_id: str
    doc_entry: str
    line_num: int = 0
    doc_number: str | None = None      # human-facing invoice no., used for dedup
    posting_date: str | None = None    # ISO date string as exported
    item_code: str | None = None
    item_name: str | None = None
    vendor_name: str | None = None
    gl_account: str | None = None
    quantity: Decimal | None = None
    uom: str | None = None             # unit of measure, e.g. L, kWh, KG
    amount: Decimal | None = None      # net line amount (currency)
    currency: str = "MYR"
    raw: dict[str, str] = Field(default_factory=dict)

    @property
    def line_key(self) -> tuple[str, str, int]:
        return (self.client_id, self.doc_entry, self.line_num)


# --------------------------------------------------------------------------- #
# Rules (FR-S3)
# --------------------------------------------------------------------------- #
class MappingRule(_Frozen):
    """A single carbon mapping rule within a versioned ruleset.

    ``match`` is keyed by one of the precedence dimensions (item/vendor/gl);
    the engine applies item rules before vendor rules before GL rules. A
    matched rule fixes the carbon category, scope, quantity basis and the
    emission-factor reference to use.
    """

    rule_id: str
    # exactly one of these is set — checked by the ruleset loader
    item_code: str | None = None
    vendor_match: str | None = None    # case-insensitive substring on vendor_name
    gl_account: str | None = None

    category: str                      # carbon category label, e.g. "Fleet diesel"
    scope: Scope
    basis: QuantityBasis               # preferred basis if data supports it
    factor_ref: str                    # key into the factor registry
    # EEIO factor used when an ACTIVITY rule has no usable line quantity.
    spend_fallback_ref: str | None = None

    @property
    def dimension(self) -> str:
        if self.item_code is not None:
            return "item"
        if self.vendor_match is not None:
            return "vendor"
        return "gl"


class RuleSet(_Frozen):
    """A versioned, client-scoped collection of mapping rules.

    ``version`` is recorded on every EmissionEntry the ruleset produces, so an
    auditor can replay exactly which rules classified a given figure.
    """

    client_id: str
    version: str                       # e.g. "v7"
    rules: tuple[MappingRule, ...]


# --------------------------------------------------------------------------- #
# Emissions
# --------------------------------------------------------------------------- #
class EmissionEntry(_Frozen):
    """The carbon result for one source line — the unit the release gate hashes."""

    line_key: tuple[str, str, int]
    doc_entry: str
    line_num: int
    doc_number: str | None
    category: str
    scope: Scope
    basis: QuantityBasis
    data_quality: DataQuality
    quantity: Decimal | None           # activity quantity (None for pure spend)
    uom: str | None
    amount: Decimal | None             # spend amount used (if spend basis)
    factor_ref: str
    factor_value: Decimal              # the numeric factor applied
    factor_version: str                # factor-set version, for replay
    rule_id: str
    rule_version: str                  # ruleset version that classified this line
    tco2e: Decimal                     # final emissions, tonnes CO2e
    source_hash: str                   # SHA-256 of the source line snapshot
    notes: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Validation / batch
# --------------------------------------------------------------------------- #
class RowOutcome(_Frozen):
    """Per-row result of validation, before commit."""

    row_index: int                     # 1-based position in the source file
    status: RowStatus
    line_key: tuple[str, str, int] | None
    messages: tuple[str, ...] = ()


class ValidationReport(_Frozen):
    """The import validation report shown before commit (FR-S1)."""

    file_name: str
    file_sha256: str
    total_rows: int
    outcomes: tuple[RowOutcome, ...]

    def _count(self, status: RowStatus) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def clean(self) -> int:
        return self._count(RowStatus.CLEAN)

    @property
    def warnings(self) -> int:
        return self._count(RowStatus.WARNING)

    @property
    def duplicates(self) -> int:
        return self._count(RowStatus.DUPLICATE)

    @property
    def rejected(self) -> int:
        return self._count(RowStatus.REJECTED)

    @property
    def committable(self) -> bool:
        """Whole-batch policy: any rejected (malformed) row blocks commit."""
        return self.rejected == 0


class DuplicateHit(_Frozen):
    """A cross-channel duplicate finding (FR-S8)."""

    line_key: tuple[str, str, int]
    doc_number: str
    category: str
    owning_channel: Channel
    other_channel: Channel
    reason: str
