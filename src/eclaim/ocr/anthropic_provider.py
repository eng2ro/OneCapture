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
confidence (number 0..1).
Rules: fuel pump receipt -> fuel_diesel/fuel_petrol by product (RON95/97=petrol; diesel/B7/B10=diesel),
quantity = litres. Electricity bill (e.g. Tenaga Nasional/TNB) -> electricity, quantity = kWh.
Strip thousands separators. Use null where a value is not printed.
Return ONLY the JSON object, no prose, no code fences."""


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()


class AnthropicVisionProvider(OcrProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.anthropic_api_key
        self._model = model or settings.ocr_model

    def extract(self, image_bytes: bytes, media_type: str) -> Extraction:
        # Imported lazily so the package imports without the SDK / a key present.
        from anthropic import Anthropic

        client = Anthropic(api_key=self._api_key)
        try:
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
            return Extraction.model_validate(json.loads(_strip_fences(raw)))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise OcrError(f"could not parse OCR response: {exc}") from exc
        except Exception as exc:  # transport / API failure
            raise OcrError(f"OCR request failed: {exc}") from exc
