"""Shared release primitives: canonical batch hashing + external seams.

The release gate's deterministic anchor is a SHA-256 over the canonical,
key-sorted JSON of every released entry. The *projection* (which fields define
the carbon claim) differs per module — ERP Sync hashes invoice-line entries,
e-Claim hashes claims — but the canonicalisation is identical, so it lives here
in :func:`canonical_hash`.

The two external steps a real release performs — RFC 3161 TSA stamping and
Carbon Next ingestion — are seams: :class:`TimestampAuthority` / :class:`ReleaseSink`
protocols with no-network stubs. Wiring real services in never changes the hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def canonical_hash(
    items: list[dict], *, sort_key: Callable[[dict], Any] | None = None
) -> str:
    """Deterministic, order-independent SHA-256 over a list of projections.

    Each ``item`` must already be a JSON-safe dict (Decimals rendered as strings
    by the caller). Items are sorted — by ``sort_key`` if given, else by their
    own canonical JSON — then serialised canonically and hashed, so the same set
    always yields the same digest regardless of input order.
    """
    key = sort_key or _canonical_json
    canon = sorted(items, key=key)
    return hashlib.sha256(_canonical_json(canon).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# External seams (stubbed until the real services land)
# --------------------------------------------------------------------------- #
class TimestampAuthority(Protocol):
    """RFC 3161 TSA seam. Real impl returns a verifiable token over the hash."""

    def stamp(self, digest_hex: str) -> str: ...


class ReleaseSink(Protocol):
    """Carbon Next ingestion seam. Real impl posts the released batch."""

    def post(self, batch_hash_hex: str, count: int) -> str: ...


@dataclass
class StubTSA:
    """No-network TSA stub: deterministic pseudo-token, clearly marked."""

    def stamp(self, digest_hex: str) -> str:
        return f"STUB-TSA:{digest_hex[:16]}"


@dataclass
class StubSink:
    """No-network sink stub: records what *would* be posted."""

    posted: list[str] = field(default_factory=list)

    def post(self, batch_hash_hex: str, count: int) -> str:
        receipt = f"STUB-CARBONNEXT:{batch_hash_hex[:16]}:{count}"
        self.posted.append(receipt)
        return receipt
