"""OCR provider interface and the structured extraction it returns."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

ExpenseType = Literal[
    "fuel_diesel", "fuel_petrol", "electricity", "natural_gas", "air_travel", "other"
]
Unit = Literal["L", "kWh", "m3", "km"]

# What KIND of document this is — the classifier output that the router (C1) uses to
# send a captured page to the right queue: a staff-paid ``expense_receipt`` into
# e-Claim; a ``vendor_invoice`` (the bill finance actually pays) — the only AP-side
# type that is PAYABLE; its ``delivery_order``; a supplier ``quotation`` (a price
# offer, NOT payable) or a ``purchase_order`` (an order, not itself a bill); or
# ``unknown``. Defaults to ``expense_receipt`` so a provider that predates the
# classifier (and the fake OCR in tests) keeps the existing e-Claim behaviour.
DocumentType = Literal[
    "expense_receipt", "vendor_invoice", "delivery_order",
    "quotation", "purchase_order", "unknown",
]


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
    # Tax read off the document: the GST/SST amount shown, and its code (Malaysia: SR
    # standard-rated, ZR zero-rated, ES exempt). Both None when the document prints no
    # separate tax — the reviewer can still key them. ``total_amount`` stays the GROSS.
    tax_amount: Decimal | None = None
    tax_code: str | None = None
    expense_type: ExpenseType = "other"
    quantity: Decimal | None = None
    unit: Unit | None = None
    confidence: Decimal | None = None
    # Document classification (C1): what kind of document this is, how sure the model
    # is (0..1), and the human-readable cues it used (shown in the review UI + audit).
    # ``document_type`` defaults to ``expense_receipt`` so nothing that predates the
    # classifier changes behaviour; ``type_confidence`` None means "unclassified".
    document_type: DocumentType = "expense_receipt"
    type_confidence: Decimal | None = None
    type_signals: list[str] = []
    # The PO / DO reference PRINTED on the document (not its own doc_no) — the key that
    # links a delivery_order to its matching vendor_invoice (same vendor + po_ref). None
    # when no such cross-reference is present (a plain receipt).
    po_ref: str | None = None
    # Per-field bounding boxes on the receipt image, NORMALIZED to 0..1 as
    # ``[x, y, w, h]`` (origin top-left). Field name -> box. Optional and
    # provider-agnostic: the vision OCR returns approximate boxes; a precise
    # document-AI provider can populate the same shape later. ``None`` (or a
    # missing field) simply means "no highlight available" — never an error.
    boxes: dict[str, list[float]] | None = None


class OcrProvider(Protocol):
    """Reads a receipt image into a structured :class:`Extraction`."""

    def extract(self, image_bytes: bytes, media_type: str) -> Extraction: ...
