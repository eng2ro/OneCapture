"""File-level batch helpers: stable SHA-256 of the source file (evidence) and
the canonical SHA-256 of a single source line snapshot (used by the batch hash
and stored on every emission entry for replay).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..domain.models import SourceRecord


def file_sha256(path: str | Path) -> str:
    """SHA-256 of the raw file bytes — the source evidence anchor."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def source_snapshot_hash(record: SourceRecord) -> str:
    """Deterministic hash of a source line's raw cell map.

    Uses the untouched ``raw`` dict so the hash reflects exactly what the ERP
    exported, independent of how we later parse it. Sorted keys make it
    reproducible across runs and platforms.
    """
    payload = {
        "line_key": list(record.line_key),
        "raw": record.raw,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
