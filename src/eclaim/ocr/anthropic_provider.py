"""Anthropic vision OCR provider (model ``claude-sonnet-4-6``).

Sends the receipt image with a strict instruction to return *only* JSON, strips
any ``` fences, and validates into an :class:`Extraction`. Any transport or
parse failure raises :class:`OcrError` so the claim is never partially saved.

NEVER exercised in CI — tests inject a fake provider.
"""

from __future__ import annotations

import base64
import json

from pydantic import ValidationError

from ..config import get_settings
from .base import Extraction, OcrError, OcrProvider

_INSTRUCTION = """\
Extract this receipt as JSON with exactly these keys:
vendor (string), doc_no (string|null), date (string|null), currency (string|null),
total_amount (number|null), expense_type ("fuel_diesel"|"fuel_petrol"|"electricity"|
"natural_gas"|"air_travel"|"other"), quantity (number|null), unit ("L"|"kWh"|"m3"|"km"|null),
confidence (number 0..1),
boxes (object|null).
Rules: fuel pump receipt -> fuel_diesel/fuel_petrol by product (RON95/97=petrol; diesel/B7/B10=diesel),
quantity = litres. Electricity bill (e.g. Tenaga Nasional/TNB) -> electricity, quantity = kWh.
Strip thousands separators. Use null where a value is not printed.
"boxes" maps each non-null field above (vendor, doc_no, date, total_amount, quantity, ...)
to the bounding box of the EXACT printed text you took that value from, as [x, y, w, h]
NORMALIZED to 0..1, origin TOP-LEFT (x right, y down). The box must TIGHTLY enclose only
that value's characters — not the whole line, not a nearby label. For a fuel receipt the
"quantity" box is the dispensed VOLUME number (e.g. "34.146" or "34.146 L" / "LITRES"),
NOT the price, unit price, or pump number. Double-check each box visually covers the value
you reported. Omit a field from "boxes" if you cannot confidently locate it; use null if
you cannot produce boxes at all.
Return ONLY the JSON object, no prose, no code fences."""


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
            extraction = Extraction.model_validate(data)
            return extraction.model_copy(update={"boxes": boxes}) if boxes else extraction
        except (json.JSONDecodeError, ValidationError) as exc:
            raise OcrError(f"could not parse OCR response: {exc}") from exc
        except Exception as exc:  # transport / API failure
            raise OcrError(f"OCR request failed: {exc}") from exc
