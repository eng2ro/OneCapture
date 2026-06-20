"""Closed enumerations used across the ERP Sync pipeline.

These intentionally use string values so they serialise stably into the
canonical batch hash (release/gate.py) and the JSON persistence store.
Changing a string value changes the release hash, so treat them as a
versioned contract.
"""

from __future__ import annotations

from enum import StrEnum


class Scope(StrEnum):
    """GHG Protocol scope of an emission entry.

    S3_4 (upstream transport & distribution) and S3_11 (use of sold products)
    are the two Scope 3 categories the spec calls out by number; other Scope 3
    lines collapse to S3_OTHER until the Phase 2 product-factor master lands.
    """

    S1 = "scope_1"          # direct combustion — fleet/bulk fuel, refrigerant
    S2 = "scope_2"          # purchased electricity (location-based, MY grid)
    S3_4 = "scope_3_4"      # upstream transport & distribution
    S3_11 = "scope_3_11"    # use of sold products (Phase 2 factor master)
    S3_OTHER = "scope_3_other"


class QuantityBasis(StrEnum):
    """How the activity quantity behind an emission entry was obtained.

    ACTIVITY is preferred (litres/kWh/kg straight off the invoice line);
    SPEND is the EEIO fallback when no usable line quantity exists, and any
    SPEND entry is always data-quality flagged.
    """

    ACTIVITY = "activity"
    SPEND = "spend"


class DataQuality(StrEnum):
    """Confidence tag carried on every emission entry, surfaced to review."""

    MEASURED = "measured"        # activity qty from a line with a known UoM
    ESTIMATED = "estimated"      # spend-based / EEIO fallback
    FLAGGED = "flagged"          # measured but something needs a human look


class RowStatus(StrEnum):
    """Validation classification of a single source row.

    Mirrors the prototype's import validation report buckets.
    """

    CLEAN = "clean"            # parses, maps, has a usable quantity basis
    WARNING = "warning"        # commits, but staged spend-based / DQ-flagged
    DUPLICATE = "duplicate"    # already seen (idempotency) — skipped on commit
    REJECTED = "rejected"      # malformed — blocks the whole-batch commit


class BatchStatus(StrEnum):
    """Lifecycle of an import batch."""

    REJECTED = "rejected"      # had malformed rows; nothing committed
    STAGED = "staged"          # committed to the store, hash computed
    RELEASED = "released"      # TSA-anchored + posted (Phase 1: seam only)


# Channels that can own a carbon category (FR-S8 cross-channel dedup).
class Channel(StrEnum):
    ERP_SYNC = "erp_sync"
    E_CLAIM = "e_claim"
