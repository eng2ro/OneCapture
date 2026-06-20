"""Release gate (FR-S6).

The release gate's job in pass 1 is the *deterministic batch hash*: a SHA-256
over the canonical, sorted JSON of every staged emission entry. The same set
of entries always produces the same hash, and any change to a carbon-relevant
field changes it — that hash is what the irreversible release later anchors to
an RFC 3161 TSA and posts to Carbon Next.

Those two external steps (TSA stamping, Carbon Next ingestion) are **out of
pass 1**. They live here as an explicit seam — :class:`ReleaseGate` with a
pluggable ``tsa`` and ``sink`` — defaulting to no-op stubs that record intent
without contacting anything. Wiring real services in does not change the hash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from core.release import (
    ReleaseSink,
    StubSink,
    StubTSA,
    TimestampAuthority,
    canonical_hash,
)

from ..domain.enums import BatchStatus
from ..domain.models import EmissionEntry

# Re-exported for callers that import the seams from the ERP Sync gate.
__all__ = [
    "ReleaseSink",
    "StubSink",
    "StubTSA",
    "TimestampAuthority",
    "batch_hash",
    "ReleaseGate",
    "ReleaseResult",
]


def _entry_canonical(entry: EmissionEntry) -> dict:
    """Carbon-relevant, hash-stable projection of an emission entry.

    Decimals are rendered as plain strings so the hash never depends on float
    formatting. Only fields that define the carbon claim are included; volatile
    metadata (notes) is excluded so re-running review text doesn't break the
    anchor.
    """
    return {
        "line_key": list(entry.line_key),
        "doc_number": entry.doc_number,
        "category": entry.category,
        "scope": entry.scope.value,
        "basis": entry.basis.value,
        "data_quality": entry.data_quality.value,
        "quantity": _dec(entry.quantity),
        "uom": entry.uom,
        "amount": _dec(entry.amount),
        "factor_ref": entry.factor_ref,
        "factor_value": _dec(entry.factor_value),
        "factor_version": entry.factor_version,
        "rule_id": entry.rule_id,
        "rule_version": entry.rule_version,
        "tco2e": _dec(entry.tco2e),
        "source_hash": entry.source_hash,
    }


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def batch_hash(entries: list[EmissionEntry]) -> str:
    """Deterministic SHA-256 over a set of staged entries (order-independent)."""
    return canonical_hash(
        [_entry_canonical(e) for e in entries],
        sort_key=lambda d: d["line_key"],
    )


@dataclass(frozen=True)
class ReleaseResult:
    status: BatchStatus
    batch_hash: str
    tsa_token: str | None
    sink_receipt: str | None


@dataclass
class ReleaseGate:
    """Computes the batch hash and (in real builds) anchors + posts it.

    Pass 1 wires the default stubs, so ``release`` computes the hash and returns
    a clearly-marked stub token/receipt without any network call. The
    irreversibility and TSA/Carbon-Next steps are deliberately not implemented.
    """

    tsa: TimestampAuthority = field(default_factory=StubTSA)
    sink: ReleaseSink = field(default_factory=StubSink)

    def compute_hash(self, entries: list[EmissionEntry]) -> str:
        return batch_hash(entries)

    def release(self, entries: list[EmissionEntry]) -> ReleaseResult:
        digest = self.compute_hash(entries)
        token = self.tsa.stamp(digest)
        receipt = self.sink.post(digest, len(entries))
        return ReleaseResult(
            status=BatchStatus.RELEASED,
            batch_hash=digest,
            tsa_token=token,
            sink_receipt=receipt,
        )
