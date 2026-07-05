"""Phase-4 LLM page-segmentation of a multi-invoice PDF (DB-backed, fake segmenter).

With document split ON, a PDF's pages are grouped into invoices: a multi-page group
becomes ONE line (stitched composite) whose constituent pages are retained so the
reviewer can still split it; single-page groups become one line each. The real
Anthropic segmenter is never called in tests — a fake returns the grouping.
"""

from __future__ import annotations

import json
import re
import uuid

from fpdf import FPDF
from sqlalchemy import select

from eclaim.db.models import Client, ClaimLine
from eclaim.ocr.segment import (
    SEG_MAX_PAGES_PER_BATCH,
    _is_ordered_partition,
    _merge_partitions,
    _plan_batches,
    one_per_page,
)


def _pdf(n_pages: int) -> bytes:
    doc = FPDF()
    for i in range(n_pages):
        doc.add_page()
        doc.set_font("Helvetica", size=20)
        doc.cell(0, 20, f"Invoice page {i + 1}")
    return bytes(doc.output())


def _lines(db_session, claim_id):
    return db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == claim_id).order_by(ClaimLine.line_no)
    ).scalars().all()


def _enable_split(db_session):
    cid = db_session.info["principal"]["client"]
    cl = db_session.get(Client, cid)
    cl.modules = {**(cl.modules or {}), "allow_document_split": True}
    db_session.flush()


def _capture(client, n_pages):
    resp = client.post(
        "/capture",
        files=[("files", ("inv.pdf", _pdf(n_pages), "application/pdf"))],
        data={"items": "[null]"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text[:400]
    cid = re.match(r"^/claims/([0-9a-f-]+)/review$", resp.headers["location"]).group(1)
    return uuid.UUID(cid)


# --- the partition validator (pure) -----------------------------------------
def test_partition_validator_accepts_contiguous_and_rejects_bad():
    assert _is_ordered_partition([[0, 1], [2]], 3)
    assert _is_ordered_partition([[0], [1], [2]], 3)
    assert not _is_ordered_partition([[0, 2], [1]], 3)   # out of order
    assert not _is_ordered_partition([[0, 1]], 3)        # page 2 dropped
    assert not _is_ordered_partition([[0, 0, 1, 2]], 3)  # duplicate
    assert not _is_ordered_partition([], 3)
    assert one_per_page(3) == [[0], [1], [2]]


# --- batching + merge for large PDFs (the oversize-safe path) ---------------
def test_plan_batches_overlaps_by_one_and_covers_every_pair():
    # Small PDF: a single batch.
    assert _plan_batches(5, 20) == [(0, 4)]
    assert _plan_batches(1, 20) == [(0, 0)]
    assert _plan_batches(0, 20) == []

    # Large PDF: consecutive batches share exactly one page (the overlap), and
    # every adjacent pair (i, i+1) is fully inside some batch.
    n, mx = 30, 20
    ranges = _plan_batches(n, mx)
    assert ranges == [(0, 19), (19, 29)]
    for start, end in ranges:
        assert end - start + 1 <= mx
    covered = set()
    for start, end in ranges:
        covered.update(range(start, end))   # pairs (p, p+1) for p in [start, end)
    assert covered == set(range(n - 1))      # all pairs covered, none missing


def test_merge_partitions_stitches_across_batches():
    # Two batches over 5 pages, overlap page 2. Batch A: {0} {1,2}; batch B: {2} {3,4}.
    ranges = [(0, 2), (2, 4)]
    parts = [[[0], [1, 2]], [[2], [3, 4]]]
    assert _merge_partitions(5, ranges, parts) == [[0], [1, 2], [3, 4]]


def test_merge_partitions_failed_batch_is_local_one_per_page():
    # Batch B failed (None) → only ITS pages fall back to one-per-page; batch A's
    # grouping (pages 0-1 are one document) is preserved.
    ranges = [(0, 2), (2, 4)]
    parts = [[[0, 1, 2]], None]
    assert _merge_partitions(5, ranges, parts) == [[0, 1, 2], [3], [4]]


def test_batch_boundary_grouping_survives_the_merge():
    # An invoice that spans the batch boundary (pages 19-20) stays ONE group.
    n = 30
    ranges = _plan_batches(n, SEG_MAX_PAGES_PER_BATCH)   # [(0,19),(19,29)]
    a = [[i] for i in range(0, 19)] + [[19]]             # batch A: pages 0..19 all singletons
    b = [[19, 20]] + [[i] for i in range(21, 30)]        # batch B: pages 19+20 together
    merged = _merge_partitions(n, ranges, [a, b])
    assert [19, 20] in merged
    assert _is_ordered_partition(merged, n)


# --- segmentation applied at capture ----------------------------------------
def test_grouped_pages_become_one_line_with_retained_pages(client, db_session, fake_segmenter):
    _enable_split(db_session)
    fake_segmenter.groups = [[0, 1], [2]]     # pages 1+2 are one invoice; page 3 another
    cid = _capture(client, 3)

    lines = _lines(db_session, cid)
    assert len(lines) == 2                     # two invoices, not three pages
    assert lines[0].pages is not None and len(lines[0].pages) == 2   # merged group keeps its pages
    assert lines[1].pages is None              # single-page group is an ordinary line


def test_one_per_page_grouping_gives_a_line_per_page(client, db_session, fake_segmenter):
    _enable_split(db_session)
    fake_segmenter.groups = None               # default: one page per group
    cid = _capture(client, 3)
    lines = _lines(db_session, cid)
    assert len(lines) == 3
    assert all(ln.pages is None for ln in lines)


def test_segmented_multipage_line_is_splittable_again(client, db_session, fake_segmenter):
    _enable_split(db_session)
    fake_segmenter.groups = [[0, 1, 2]]        # model says all three pages = one invoice
    cid = _capture(client, 3)
    lines = _lines(db_session, cid)
    assert len(lines) == 1 and len(lines[0].pages) == 3

    # The reviewer disagrees → split it back into three.
    resp = client.post(
        f"/claims/{cid}/lines/split",
        data={"line_id": str(lines[0].id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert len(_lines(db_session, cid)) == 3
