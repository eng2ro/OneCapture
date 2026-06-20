"""Column presets: map a client's exported header names onto our canonical
field names. FR-S1 standardises on a "SAP B1 AP listing v1" shape, but every
client's export differs slightly, so presets are per-client YAML overrides on
top of a built-in default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Canonical fields the rest of the pipeline understands.
CANONICAL_FIELDS = (
    "doc_entry",
    "line_num",
    "doc_number",
    "posting_date",
    "item_code",
    "item_name",
    "vendor_name",
    "gl_account",
    "quantity",
    "uom",
    "amount",
    "currency",
)

# Required to even attempt a row: without these we cannot identify or value it.
REQUIRED_FIELDS = ("doc_entry", "vendor_name", "amount")


# The built-in "SAP B1 AP listing v1" mapping (canonical_field -> source header).
DEFAULT_PRESET: dict[str, str] = {
    "doc_entry": "DocEntry",
    "line_num": "LineNum",
    "doc_number": "DocNum",
    "posting_date": "DocDate",
    "item_code": "ItemCode",
    "item_name": "ItemName",
    "vendor_name": "CardName",
    "gl_account": "AcctCode",
    "quantity": "Quantity",
    "uom": "UoM",
    "amount": "LineTotal",
    "currency": "Currency",
}


@dataclass(frozen=True)
class ColumnPreset:
    """Maps source headers to canonical fields for one client."""

    client_id: str
    mapping: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PRESET))

    def source_header_for(self, canonical: str) -> str | None:
        return self.mapping.get(canonical)

    @classmethod
    def default(cls, client_id: str) -> "ColumnPreset":
        return cls(client_id=client_id, mapping=dict(DEFAULT_PRESET))

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ColumnPreset":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        client_id = str(data["client_id"])
        mapping = dict(DEFAULT_PRESET)
        mapping.update(data.get("columns", {}))
        unknown = set(mapping) - set(CANONICAL_FIELDS)
        if unknown:
            raise ValueError(f"Preset {client_id}: unknown canonical fields {sorted(unknown)}")
        return cls(client_id=client_id, mapping=mapping)
