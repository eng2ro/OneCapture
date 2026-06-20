"""OCR seam: an :class:`OcrProvider` interface and the Anthropic vision impl.

The interface lets a dedicated OCR vendor swap in later (decision D4) without
touching the services. Tests inject a fake provider — the real one never runs
in CI.
"""

from .base import Extraction, OcrError, OcrProvider

__all__ = ["Extraction", "OcrProvider", "OcrError"]
