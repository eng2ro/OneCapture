"""Anthropic vision OCR provider (model ``claude-sonnet-4-6``).

Sends the receipt image with a strict instruction to return *only* JSON, strips
any ``` fences, and validates into an :class:`Extraction`. Any transport or
parse failure raises :class:`OcrError` so the claim is never partially saved.

NEVER exercised in CI — tests inject a fake provider.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from ..config import get_settings
from .base import Extraction, OcrError, OcrProvider

_INSTRUCTION = """\
Extract this document as JSON with exactly these keys:
vendor (string), doc_no (string|null), date (string|null), currency (string|null),
total_amount (number|null), expense_type ("fuel_diesel"|"fuel_petrol"|"electricity"|
"natural_gas"|"air_travel"|"other"), quantity (number|null), unit ("L"|"kWh"|"m3"|"km"|null),
confidence (number 0..1),
document_type ("expense_receipt"|"vendor_invoice"|"delivery_order"|"quotation"|"purchase_order"|"unknown"),
type_confidence (number 0..1),
type_signals (array of short strings),
po_ref (string|null),
boxes (object|null).
Rules: fuel pump receipt -> fuel_diesel/fuel_petrol by product (RON95/97=petrol; diesel/B7/B10=diesel),
quantity = litres. Electricity bill (e.g. Tenaga Nasional/TNB) -> electricity, quantity = kWh.
Strip thousands separators. Use null where a value is not printed.

Classify document_type by these cues, and list the ones you actually saw in type_signals:
- expense_receipt: a paid receipt / card slip / cash sale (has "Receipt", "Cash", "Change",
  a card approval code); something a person paid and would claim back.
- vendor_invoice: a bill addressed TO the customer company from an external vendor —
  a letterhead + "Bill To" naming the company, "Tax Invoice", "Payment Terms", "Due Date",
  "Bank Account"/remittance details, a PO reference. A bill finance still has to PAY.
- delivery_order: a "Delivery Order"/"DO" or goods-received note listing quantities
  delivered, usually with a DO/PO reference and NO amount due.
- quotation: a supplier's PRICE OFFER — titled "Quotation"/"Quote"/"Proforma", has a
  "Valid until"/"Validity" period, explicitly NOT a tax invoice, no amount actually
  DUE yet. A quotation is NOT a bill to pay.
- purchase_order: an ORDER from the buyer to the supplier — titled "Purchase Order"/
  "PO", authorises a purchase. It is not itself a payable bill (its later invoice is).
- unknown: you genuinely cannot tell.
Only a vendor_invoice is PAYABLE. A "Quotation"/"Valid until"/"This is not a tax
invoice" => quotation. A "Purchase Order"/"PO No" issued BY the customer => purchase_order.
Payment terms / a due date / an amount DUE / remittance bank details => vendor_invoice
(a receipt is already paid). A PO or DO reference present => AP side.
Set type_confidence to how sure you are (0..1). Set po_ref to the referenced PO/DO
number if the document cites one (e.g. "PO No: 4500012345", "Your DO: DO-778"), else null.

"boxes" maps each non-null field above (vendor, doc_no, date, total_amount, quantity, ...)
to the bounding box of the EXACT printed text you took that value from, as [x, y, w, h]
NORMALIZED to 0..1, origin TOP-LEFT (x right, y down). The box must TIGHTLY enclose only
that value's characters — not the whole line, not a nearby label. For a fuel receipt the
"quantity" box is the dispensed VOLUME number (e.g. "34.146" or "34.146 L" / "LITRES"),
NOT the price, unit price, or pump number. Double-check each box visually covers the value
you reported. Omit a field from "boxes" if you cannot confidently locate it; use null if
you cannot produce boxes at all.
Return ONLY the JSON object, no prose, no code fences."""

_VALID_DOC_TYPES = {
    "expense_receipt", "vendor_invoice", "delivery_order",
    "quotation", "purchase_order", "unknown",
}


def _coerce_classification(data: dict) -> None:
    """Normalize the classifier fields IN PLACE so a stray value degrades gracefully
    instead of failing the whole read (mirrors the tolerant ``boxes`` handling):

    * an unrecognised/absent ``document_type`` becomes ``"unknown"`` — never a
      validation error, and never a wrong confident class;
    * ``type_signals`` is coerced to a list of short strings (or dropped);
    * ``type_confidence`` that isn't a parseable number is dropped to ``None`` — a junk
      value like ``"very sure"`` is a str, so a bare isinstance check let it through to
      the Decimal validator, which raised and killed the whole page read (F4).
    """
    dt = data.get("document_type")
    if dt not in _VALID_DOC_TYPES:
        data["document_type"] = "unknown"
    signals = data.get("type_signals")
    if isinstance(signals, (list, tuple)):
        data["type_signals"] = [str(s)[:120] for s in signals if str(s).strip()][:12]
    elif signals is not None:
        data.pop("type_signals", None)
    tc = data.get("type_confidence")
    if tc is not None:
        try:
            Decimal(str(tc))            # numeric string / int / float → keep as-is
        except (InvalidOperation, ValueError, TypeError):
            data["type_confidence"] = None


# The SDK retries transient failures (429 rate-limit, 529 overloaded, 5xx, and
# connection/timeout blips) with exponential backoff on its own. It does NOT retry a
# 400 invalid_request (bad input or "credit balance too low"), which is correct —
# retrying those never helps. A generous timeout guards a single hung read.
_MAX_RETRIES = 4
_TIMEOUT_SECONDS = 60.0


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()


def _coerce_boxes(raw) -> dict[str, list[float]] | None:
    """Keep only well-formed boxes — field -> [x, y, w, h] of 4 numbers clamped to
    0..1. Tolerant by design: a malformed box (or the whole ``boxes`` object) is
    dropped, never raised, so the bounding-box overlay degrades gracefully without
    failing the receipt read."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, list[float]] = {}
    for field, box in raw.items():
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            coords = [max(0.0, min(1.0, float(v))) for v in box]
        except (TypeError, ValueError):
            continue
        out[str(field)] = coords
    return out or None


class AnthropicVisionProvider(OcrProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.anthropic_api_key
        self._model = model or settings.ocr_model

    def extract(self, image_bytes: bytes, media_type: str) -> Extraction:
        try:
            # Imported + constructed inside the try so a missing SDK or an
            # unconfigured/invalid key surfaces as OcrError (the documented
            # contract) rather than escaping — callers degrade to manual entry.
            from anthropic import Anthropic

            client = Anthropic(
                api_key=self._api_key, max_retries=_MAX_RETRIES, timeout=_TIMEOUT_SECONDS
            )
            message = client.messages.create(
                model=self._model,
                max_tokens=1024,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": base64.b64encode(image_bytes).decode(),
                                },
                            },
                            {"type": "text", "text": _INSTRUCTION},
                        ],
                    }
                ],
            )
            raw = "".join(block.text for block in message.content if block.type == "text")
            data = json.loads(_strip_fences(raw))
            # Pull boxes out and re-attach after tolerant coercion, so a malformed
            # box object can never fail the field extraction itself.
            boxes = _coerce_boxes(data.pop("boxes", None)) if isinstance(data, dict) else None
            if isinstance(data, dict):
                _coerce_classification(data)
            extraction = Extraction.model_validate(data)
            return extraction.model_copy(update={"boxes": boxes}) if boxes else extraction
        except (json.JSONDecodeError, ValidationError) as exc:
            raise OcrError(f"could not parse OCR response: {exc}") from exc
        except Exception as exc:  # transport / API failure
            raise OcrError(f"OCR request failed: {exc}") from exc
