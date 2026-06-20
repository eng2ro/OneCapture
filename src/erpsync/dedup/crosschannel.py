"""Cross-channel duplicate screening (FR-S8).

Double counting is designed out two ways:

1. **Category ownership matrix** — each carbon category belongs to exactly one
   channel for a given client (e.g. staff fuel claims -> e-Claim; fleet/bulk
   fuel, utilities, refrigerant -> ERP Sync). An ERP Sync entry whose category
   is owned by e-Claim is a boundary violation and is flagged.

2. **Document-number match** — if an ERP Sync entry shares an invoice/document
   number with a claim already captured in e-Claim, it is a duplicate of that
   claim regardless of category.

This module is pure: it takes the ownership matrix and a view of e-Claim's
already-captured document numbers, and returns the hits. Wiring those views to
the real e-Claim store is a Phase-2 connector concern; the seam is the
``eclaim_doc_numbers`` argument.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..domain.enums import Channel
from ..domain.models import DuplicateHit, EmissionEntry


@dataclass(frozen=True)
class OwnershipMatrix:
    """Maps a carbon category -> the single channel that owns it."""

    client_id: str
    owner_by_category: dict[str, Channel]

    def owner_of(self, category: str) -> Channel | None:
        return self.owner_by_category.get(category)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "OwnershipMatrix":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        owners = {
            str(cat): Channel(ch) for cat, ch in (data.get("ownership") or {}).items()
        }
        return cls(client_id=str(data["client_id"]), owner_by_category=owners)


def screen(
    entries: list[EmissionEntry],
    matrix: OwnershipMatrix,
    *,
    eclaim_doc_numbers: set[str] | None = None,
) -> list[DuplicateHit]:
    """Return cross-channel duplicate hits among ERP Sync emission entries."""
    eclaim_doc_numbers = {d.strip() for d in (eclaim_doc_numbers or set()) if d}
    hits: list[DuplicateHit] = []

    for entry in entries:
        # 1. ownership boundary: this category should not be captured by ERP Sync
        owner = matrix.owner_of(entry.category)
        if owner is not None and owner is not Channel.ERP_SYNC:
            hits.append(
                DuplicateHit(
                    line_key=entry.line_key,
                    doc_number=entry.doc_number or "",
                    category=entry.category,
                    owning_channel=owner,
                    other_channel=Channel.ERP_SYNC,
                    reason=f"category {entry.category!r} is owned by {owner.value}, not ERP Sync",
                )
            )
            continue  # one finding per entry is enough to hold it back

        # 2. document-number collision with an existing e-Claim capture
        if entry.doc_number and entry.doc_number.strip() in eclaim_doc_numbers:
            hits.append(
                DuplicateHit(
                    line_key=entry.line_key,
                    doc_number=entry.doc_number.strip(),
                    category=entry.category,
                    owning_channel=Channel.E_CLAIM,
                    other_channel=Channel.ERP_SYNC,
                    reason=f"document {entry.doc_number!r} already captured in e-Claim",
                )
            )

    return hits
