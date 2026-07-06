"""LLM page-segmentation: group a PDF's rendered pages into distinct invoices.

Phase-4 refinement of PDF ingest. When document split is enabled, rather than
blindly one-line-per-page, a vision model decides which consecutive pages belong
to the same invoice — so a 3-page invoice becomes ONE line (pages grouped) and two
1-page invoices become two lines. Best-effort: any failure (no key, transport, or
a response that isn't a clean in-order partition) falls back to one page per group,
so capture is never blocked and the reviewer can still merge/split by hand.

Scaling: boundary detection only needs each page's layout (header/logo/"Page x of
y"), not its fine print, so pages are downscaled to small JPEG thumbnails for the
model call (the OCR pass still reads the full-resolution pages). Pages are also
segmented in overlapping batches so a large PDF can never exceed the request-size
limit in a single call — previously a big batch blew past the ~32 MB cap and
*silently* fell back to one-per-page, which would wrongly split a genuine
multi-page invoice. Now a failed/invalid batch falls back to one-per-page only for
its own page range (and logs it), never the whole document.

Tests inject a fake segmenter — the real one never runs in CI.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Protocol

from ..config import get_settings
from ..imaging import ImageTooLarge

logger = logging.getLogger(__name__)

# Downscale each page to at most this many pixels on its longest side before
# sending it to the segmenter. ~1024px keeps headers/logos/totals legible for
# boundary detection while shrinking a ~2 MB page render to ~100-250 KB.
SEG_THUMB_MAX_SIDE = 1024
# Pages per model call. Consecutive batches overlap by one page so every adjacent
# page-pair is judged within some batch; kept modest so many images don't dilute
# the model's attention and so the per-request payload stays small.
SEG_MAX_PAGES_PER_BATCH = 20

_INSTRUCTION = """\
You are given {n} page images of a single uploaded PDF, in order, labelled Page 0
to Page {last}. Some are separate invoices/receipts; some invoices span several
consecutive pages. Group CONSECUTIVE pages that belong to the SAME document.
Return ONLY JSON: {{"groups": [[0,1],[2], ...]}} where each inner list is the page
indices of one document, in order. Every page index 0..{last} must appear EXACTLY
once, groups must be contiguous and non-overlapping, and their order preserved.
No prose, no code fences."""


def one_per_page(n: int) -> list[list[int]]:
    """The safe fallback: each page is its own document."""
    return [[i] for i in range(n)]


def _is_ordered_partition(groups, n: int) -> bool:
    """True iff ``groups`` is an in-order, contiguous partition of range(n) — every
    page exactly once, no gaps, no overlaps. Anything else is rejected (→ fallback),
    so a bad model response can never drop or duplicate a page's evidence."""
    if not isinstance(groups, list) or not groups:
        return False
    flat: list[int] = []
    for g in groups:
        if not isinstance(g, list) or not g:
            return False
        flat.extend(g)
    return flat == list(range(n))


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()


def _thumbnail(data: bytes, *, max_side: int = SEG_THUMB_MAX_SIDE) -> bytes:
    """Downscale a page render to a small JPEG for boundary detection. On a benign
    failure (a format PIL can't thumbnail) the original bytes are returned unchanged
    (correctness over size) — but a decompression bomb (:class:`ImageTooLarge`) is
    NEVER swallowed: returning the original bytes would ship the bomb straight to the
    vision API and defeat the guard (punch-list P4). It propagates so the caller can
    reject the page / fall back safely."""
    from ..imaging import ImageTooLarge, open_guarded

    try:
        # Bomb-guarded open (also arms PIL's global pixel ceiling). ImageTooLarge is
        # raised from the header BEFORE the full raster is allocated.
        im = open_guarded(data, what="segmentation thumbnail").convert("RGB")
        w, h = im.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=70)
        return buf.getvalue()
    except ImageTooLarge:
        raise                       # never hand a bomb's bytes back to the caller
    except Exception:
        return data


def _plan_batches(n: int, max_per_batch: int) -> list[tuple[int, int]]:
    """Split range(n) into inclusive (start, end) page ranges of at most
    ``max_per_batch`` pages, with consecutive batches overlapping by ONE page.

    The overlap guarantees every adjacent pair (i, i+1) lies fully inside exactly
    one batch, so the merge can read that pair's boundary decision from a single
    batch — no cross-batch page-pair is ever left unjudged."""
    if n <= 0:
        return []
    if n <= max_per_batch:
        return [(0, n - 1)]
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < n:
        end = min(start + max_per_batch - 1, n - 1)
        ranges.append((start, end))
        if end == n - 1:
            break
        start = end   # next batch starts on this batch's last page (overlap 1)
    return ranges


def _merge_partitions(
    n: int,
    ranges: list[tuple[int, int]],
    partitions: list[list[list[int]] | None],
) -> list[list[int]]:
    """Stitch per-batch partitions (in GLOBAL page indices) into one partition of
    range(n). A boundary between pages p and p+1 is taken from whichever batch
    fully contains that pair; a batch that failed (``None``) leaves the pairs in
    its range as boundaries — i.e. a LOCAL one-per-page fallback, never global."""
    if n <= 0:
        return []
    # Default every adjacent pair to a boundary (the safe one-per-page stance).
    cut = [True] * (n - 1)
    for (start, end), part in zip(ranges, partitions):
        if part is None:
            continue   # failed batch → leave its pairs as boundaries
        page_group: dict[int, int] = {}
        for gi, grp in enumerate(part):
            for pg in grp:
                page_group[pg] = gi
        for p in range(start, end):   # adjacent pairs (p, p+1) within [start, end]
            if p in page_group and (p + 1) in page_group:
                cut[p] = page_group[p] != page_group[p + 1]
    groups: list[list[int]] = []
    cur = [0]
    for p in range(n - 1):
        if cut[p]:
            groups.append(cur)
            cur = [p + 1]
        else:
            cur.append(p + 1)
    groups.append(cur)
    return groups


class PageSegmenter(Protocol):
    """Groups ordered page images into per-document runs of page indices."""

    def segment(self, pages: list[bytes]) -> list[list[int]]: ...


class AnthropicPageSegmenter(PageSegmenter):
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.anthropic_api_key
        self._model = model or settings.ocr_model

    def segment(self, pages: list[bytes]) -> list[list[int]]:
        n = len(pages)
        if n <= 1:
            return one_per_page(n)
        try:
            from anthropic import Anthropic

            # Retry transient 429/5xx/connection blips with backoff (not 400s).
            client = Anthropic(api_key=self._api_key, max_retries=4, timeout=60.0)
        except Exception as exc:
            logger.warning(
                "segmenter: anthropic client unavailable (%s); one-per-page fallback",
                type(exc).__name__,
            )
            return one_per_page(n)

        try:
            thumbs = [_thumbnail(p) for p in pages]
        except ImageTooLarge:
            # A page exceeds the decode cap (likely a bomb). Do NOT send raw page
            # bytes to the vision API; fall back to one-per-page. The oversize page
            # is still rejected downstream by the ingestion decode guards.
            logger.warning(
                "segmenter: a page exceeds the decode cap; one-per-page fallback")
            return one_per_page(n)
        ranges = _plan_batches(n, SEG_MAX_PAGES_PER_BATCH)
        partitions: list[list[list[int]] | None] = []
        for start, end in ranges:
            local = self._segment_batch(client, thumbs[start : end + 1])
            partitions.append(
                None if local is None else [[start + i for i in g] for g in local]
            )
        return _merge_partitions(n, ranges, partitions)

    def _segment_batch(self, client, thumbs: list[bytes]) -> list[list[int]] | None:
        """Segment one batch of thumbnails. Returns a partition of range(len(thumbs))
        in LOCAL indices, or None on any error / non-partition response (→ caller
        falls back to one-per-page for this batch's range only)."""
        m = len(thumbs)
        if m <= 1:
            return [[0]] if m == 1 else []
        try:
            content: list[dict] = []
            for idx, jpg in enumerate(thumbs):
                content.append({"type": "text", "text": f"Page {idx}:"})
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.b64encode(jpg).decode(),
                    },
                })
            content.append({"type": "text", "text": _INSTRUCTION.format(n=m, last=m - 1)})
            message = client.messages.create(
                model=self._model, max_tokens=min(1024, 64 + 24 * m),
                messages=[{"role": "user", "content": content}],
            )
            raw = "".join(b.text for b in message.content if b.type == "text")
            groups = json.loads(_strip_fences(raw)).get("groups")
            groups = [[int(i) for i in g] for g in groups]
            if _is_ordered_partition(groups, m):
                return groups
            logger.warning("segmenter: non-partition for a %d-page batch; local fallback", m)
        except Exception as exc:
            logger.warning(
                "segmenter: batch of %d failed (%s); local one-per-page fallback",
                m, type(exc).__name__,
            )
        return None
