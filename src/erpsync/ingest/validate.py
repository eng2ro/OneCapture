"""Row validation and structural classification (FR-S1).

This stage decides three things per row, *before* any carbon mapping:

* ``REJECTED`` — malformed: a required field is missing or a numeric field
  cannot be parsed. Any rejected row blocks the whole-batch commit.
* ``DUPLICATE`` — the line key was already committed in a prior batch
  (idempotency) or appears earlier in this same file.
* accepted — parses into a :class:`SourceRecord`; the pipeline later refines
  it to ``CLEAN`` or ``WARNING`` once rules and quantity resolution have run.

The CLEAN/WARNING split is deferred because "warning" means *staged
spend-based / data-quality-flagged*, which is only known after the rules and
quantity stages. Keeping that out of here keeps validation purely structural.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ..domain.enums import RowStatus
from ..domain.models import RowOutcome, SourceRecord
from .column_preset import REQUIRED_FIELDS, ColumnPreset


@dataclass(frozen=True)
class ParsedRow:
    """An accepted row: its source position plus the normalised record."""

    row_index: int
    record: SourceRecord


@dataclass
class ValidationResult:
    accepted: list[ParsedRow] = field(default_factory=list)
    outcomes: list[RowOutcome] = field(default_factory=list)  # rejected + duplicate only


def _get(raw: dict[str, str], preset: ColumnPreset, canonical: str) -> str:
    header = preset.source_header_for(canonical)
    if header is None:
        return ""
    return raw.get(header, "").strip()


def _parse_decimal(text: str) -> Decimal | None:
    """Parse a money/quantity cell. Blank -> None. Bad -> raises."""
    text = text.strip()
    if text == "":
        return None
    # tolerate thousands separators and a trailing currency token
    cleaned = text.replace(",", "").replace("_", "")
    return Decimal(cleaned)


def validate_rows(
    raw_rows: list[dict[str, str]],
    preset: ColumnPreset,
    client_id: str,
    *,
    seen_keys: set[tuple[str, str, int]] | None = None,
) -> ValidationResult:
    """Validate raw rows against a preset and the idempotency key set."""
    seen_keys = set(seen_keys or set())
    result = ValidationResult()
    in_file_keys: set[tuple[str, str, int]] = set()

    for i, raw in enumerate(raw_rows, start=1):
        messages: list[str] = []

        doc_entry = _get(raw, preset, "doc_entry")
        vendor = _get(raw, preset, "vendor_name")
        amount_text = _get(raw, preset, "amount")

        # ---- required-field check ----
        missing = [
            f for f in REQUIRED_FIELDS
            if not _get(raw, preset, f)
        ]
        if missing:
            result.outcomes.append(
                RowOutcome(
                    row_index=i,
                    status=RowStatus.REJECTED,
                    line_key=None,
                    messages=(f"missing required field(s): {', '.join(missing)}",),
                )
            )
            continue

        # ---- numeric parse ----
        line_num_text = _get(raw, preset, "line_num")
        try:
            line_num = int(line_num_text) if line_num_text else 0
        except ValueError:
            result.outcomes.append(
                RowOutcome(
                    row_index=i,
                    status=RowStatus.REJECTED,
                    line_key=None,
                    messages=(f"line number is not an integer: {line_num_text!r}",),
                )
            )
            continue

        try:
            amount = _parse_decimal(amount_text)
            quantity = _parse_decimal(_get(raw, preset, "quantity"))
        except (InvalidOperation, ArithmeticError):
            result.outcomes.append(
                RowOutcome(
                    row_index=i,
                    status=RowStatus.REJECTED,
                    line_key=None,
                    messages=(
                        f"unparseable numeric value (amount={amount_text!r}, "
                        f"quantity={_get(raw, preset, 'quantity')!r})",
                    ),
                )
            )
            continue

        line_key = (client_id, doc_entry, line_num)

        # ---- duplicate detection (idempotency + in-file) ----
        if line_key in seen_keys:
            result.outcomes.append(
                RowOutcome(
                    row_index=i,
                    status=RowStatus.DUPLICATE,
                    line_key=line_key,
                    messages=("already committed in a prior batch",),
                )
            )
            continue
        if line_key in in_file_keys:
            result.outcomes.append(
                RowOutcome(
                    row_index=i,
                    status=RowStatus.DUPLICATE,
                    line_key=line_key,
                    messages=("repeated line key within this file",),
                )
            )
            continue
        in_file_keys.add(line_key)

        currency = _get(raw, preset, "currency") or "MYR"
        record = SourceRecord(
            client_id=client_id,
            doc_entry=doc_entry,
            line_num=line_num,
            doc_number=_get(raw, preset, "doc_number") or None,
            posting_date=_get(raw, preset, "posting_date") or None,
            item_code=_get(raw, preset, "item_code") or None,
            item_name=_get(raw, preset, "item_name") or None,
            vendor_name=vendor,
            gl_account=_get(raw, preset, "gl_account") or None,
            quantity=quantity,
            uom=_get(raw, preset, "uom") or None,
            amount=amount,
            currency=currency,
            raw=dict(raw),
        )
        result.accepted.append(ParsedRow(row_index=i, record=record))

    return result
