"""Shared hash-chained audit trail.

Every audited event carries ``hash = SHA-256(prev_hash + canonical(payload))``,
so the events for an entity form a tamper-evident chain: altering any earlier
event's payload breaks every following hash. This is storage-agnostic — the
caller supplies the previous hash (read from its store) and persists the result.

``payload`` should capture what the event asserts (entity id, event type, actor,
the carbon-relevant fields at that moment), never volatile data like wall-clock
time, so a verifier can recompute the chain from stored rows.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS = ""  # prev_hash of the first event in a chain


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def chain_hash(prev_hash: str | None, payload: dict) -> str:
    """SHA-256 over the previous hash followed by the canonical payload."""
    body = (prev_hash or GENESIS) + _canonical_json(payload)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def verify_chain(events: list[tuple[str | None, dict, str]]) -> bool:
    """Return True iff every (prev_hash, payload, hash) recomputes and links.

    ``events`` must be in chain order. Each event's ``prev_hash`` must equal the
    preceding event's ``hash`` (the first must be :data:`GENESIS`), and each
    ``hash`` must equal ``chain_hash(prev_hash, payload)``.
    """
    expected_prev: str | None = GENESIS
    for prev_hash, payload, digest in events:
        if (prev_hash or GENESIS) != (expected_prev or GENESIS):
            return False
        if chain_hash(prev_hash, payload) != digest:
            return False
        expected_prev = digest
    return True
