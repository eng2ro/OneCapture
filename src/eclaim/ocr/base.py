"""OCR provider interface and the structured extraction it returns."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

ExpenseType = Literal[
    "fuel_diesel", "fuel_petrol", "electricity", "natural_gas", "air_travel", "other"
]
Unit = Literal["L", "kWh", "m3", "km"]


class OcrError(RuntimeError):
    """Raised when a document can't be read (parse or transport failure).

    The caller surfaces a clean "couldn't read" error and keeps the claim
    unsaved — never a partial record.
    """


class Extraction(BaseModel):
    """What an OCR provider returns for one receipt image.

    Money/quantity are ``Decimal`` (parsed from JSON strings/numbers without
    float drift). Fields not printed on the document come back ``None``.
    """

    model_config = ConfigDict(extra="ignore")

    vendor: str | None = None
    doc_no: str | None = None
    date: str | None = None
    currency: str | None = None
    total_amount: Decimal | None = None
    expense_type: ExpenseType = "other"
    quantity: Decimal | None = None
    unit: Unit | None = None
    confidence: Decimal | None = None
    # Per-field bounding boxes on the receipt image, NORMALIZED to 0..1 as
    # ``[x, y, w, h]`` (origin top-left). Field name -> box. Optional and
    # provider-agnostic: the vision OCR returns approximate boxes; a precise
    # document-AI provider can populate the same shape later. ``None`` (or a
    # missing field) simply means "no highlight available" — never an error.
    boxes: dict[str, list[float]] | None = None


class OcrProvider(Protocol):
    """Reads a receipt image into a structured :class:`Extraction`."""

    def extract(self, image_bytes: bytes, media_type: str) -> Extraction: ...
