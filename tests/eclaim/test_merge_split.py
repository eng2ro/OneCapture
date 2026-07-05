"""Merge / split of claim lines (Phase 3) — DB-backed, fake OCR.

Merge folds several lines' page images into one line (pages of one invoice); split
expands a multi-page line back into one line per page. Both are gated by the
per-client ``allow_document_split`` policy and only while the claim is editable, and
both record an audit event.
"""

from __future__ import annotations

import io
import os
import uuid
from decimal import Decimal

import pytest
from PIL import Image
from sqlalchemy import select

from eclaim.db.models import AuditEvent, Claim, ClaimLine, Client
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimService, IllegalTransition, Repos


def _png(color=(210, 210, 210), size=(120, 180)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _lines(db_session, claim_id):
    return db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == claim_id).order_by(ClaimLine.line_no)
    ).scalars().all()


def _events(db_session, claim_id, event_type):
    return db_session.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == claim_id, AuditEvent.event_type == event_type
        )
    ).scalars().all()


def _enable_split(db_session):
    cid = db_session.info["principal"]["client"]
    cl = db_session.get(Client, cid)
    cl.modules = {**(cl.modules or {}), "allow_document_split": True}
    db_session.flush()


def _add(svc, repos, claim, fake_ocr, tmp_path, total="100"):
    fake_ocr.extraction = Extraction(expense_type="other", total_amount=Decimal(total))
    return svc.add_line(
        repos=repos, claim=claim, image_bytes=_png(), media_type="image/png",
        ocr=fake_ocr, image_dir=tmp_path,
    )


def _claim_with_lines(client, db_session, fake_ocr, tmp_path, n=3):
    """A fresh in_review claim with ``n`` single-image lines (real PNGs on disk)."""
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    claim = svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])
    for _ in range(n):
        _add(svc, repos, claim, fake_ocr, tmp_path)
    return svc, repos, claim


def test_merge_folds_lines_into_one_with_all_pages(client, db_session, fake_ocr, tmp_path):
    _enable_split(db_session)
    svc, repos, claim = _claim_with_lines(client, db_session, fake_ocr, tmp_path, n=3)
    ids = [ln.id for ln in _lines(db_session, claim.id)]

    svc.merge_lines(repos=repos, claim_id=claim.id, line_ids=ids, actor="rev", image_dir=tmp_path)

    lines = _lines(db_session, claim.id)
    assert len(lines) == 1
    survivor = lines[0]
    assert survivor.line_no == 1
    assert len(survivor.pages) == 3               # remembers its 3 constituent pages
    assert os.path.exists(survivor.image_path)    # stitched composite written
    assert len(_events(db_session, claim.id, "lines_merged")) == 1


def test_split_expands_multipage_line_back_to_pages(client, db_session, fake_ocr, tmp_path):
    _enable_split(db_session)
    svc, repos, claim = _claim_with_lines(client, db_session, fake_ocr, tmp_path, n=3)
    ids = [ln.id for ln in _lines(db_session, claim.id)]
    svc.merge_lines(repos=repos, claim_id=claim.id, line_ids=ids, actor="rev", image_dir=tmp_path)
    merged = _lines(db_session, claim.id)[0]

    svc.split_line(repos=repos, claim_id=claim.id, line_id=merged.id, actor="rev")

    lines = _lines(db_session, claim.id)
    assert len(lines) == 3                                  # back to one line per page
    assert [ln.line_no for ln in lines] == [1, 2, 3]        # contiguous
    assert all(ln.pages is None for ln in lines)
    assert len(_events(db_session, claim.id, "line_split")) == 1


def test_merge_requires_at_least_two_lines(client, db_session, fake_ocr, tmp_path):
    _enable_split(db_session)
    svc, repos, claim = _claim_with_lines(client, db_session, fake_ocr, tmp_path, n=2)
    one = _lines(db_session, claim.id)[0].id
    with pytest.raises(Exception):
        svc.merge_lines(repos=repos, claim_id=claim.id, line_ids=[one], actor="rev", image_dir=tmp_path)


def test_split_needs_a_multipage_line(client, db_session, fake_ocr, tmp_path):
    _enable_split(db_session)
    svc, repos, claim = _claim_with_lines(client, db_session, fake_ocr, tmp_path, n=1)
    single = _lines(db_session, claim.id)[0].id
    with pytest.raises(Exception):
        svc.split_line(repos=repos, claim_id=claim.id, line_id=single, actor="rev")


def test_merge_blocked_when_policy_off(client, db_session, fake_ocr, tmp_path):
    # flag NOT enabled
    svc, repos, claim = _claim_with_lines(client, db_session, fake_ocr, tmp_path, n=2)
    ids = [ln.id for ln in _lines(db_session, claim.id)]
    with pytest.raises(IllegalTransition):
        svc.merge_lines(repos=repos, claim_id=claim.id, line_ids=ids, actor="rev", image_dir=tmp_path)


def test_merge_blocked_when_claim_not_editable(client, db_session, fake_ocr, tmp_path):
    _enable_split(db_session)
    svc, repos, claim = _claim_with_lines(client, db_session, fake_ocr, tmp_path, n=2)
    ids = [ln.id for ln in _lines(db_session, claim.id)]
    claim.status = "released"
    db_session.flush()
    with pytest.raises(IllegalTransition):
        svc.merge_lines(repos=repos, claim_id=claim.id, line_ids=ids, actor="rev", image_dir=tmp_path)


# --- HTTP layer (endpoints + review-page controls) --------------------------
import re  # noqa: E402

from fpdf import FPDF  # noqa: E402


def _pdf(n_pages: int) -> bytes:
    doc = FPDF()
    for i in range(n_pages):
        doc.add_page()
        doc.set_font("Helvetica", size=20)
        doc.cell(0, 20, f"Invoice page {i + 1}")
    return bytes(doc.output())


def _capture_pdf(client, n_pages):
    """Upload an n-page PDF (split flag must be ON) → a claim with n real-image lines."""
    resp = client.post(
        "/capture",
        files=[("files", ("inv.pdf", _pdf(n_pages), "application/pdf"))],
        data={"items": "[null]"},
        follow_redirects=False,
    )
    cid = re.match(r"^/claims/([0-9a-f-]+)/review$", resp.headers["location"]).group(1)
    return uuid.UUID(cid)


def test_merge_then_split_via_http_endpoints(client, db_session):
    _enable_split(db_session)
    cid = _capture_pdf(client, 3)
    lines = _lines(db_session, cid)
    assert len(lines) == 3

    merge = client.post(
        f"/claims/{cid}/lines/merge",
        data={"line_ids": [str(ln.id) for ln in lines]},
        follow_redirects=False,
    )
    _m = re.search(r'class="error"[^>]*>([^<]+)', merge.text)   # surface the reason if it re-renders
    assert merge.status_code == 303, (_m.group(1).strip() if _m else merge.text[:500])
    merged = _lines(db_session, cid)
    assert len(merged) == 1

    split = client.post(
        f"/claims/{cid}/lines/split",
        data={"line_id": str(merged[0].id)},
        follow_redirects=False,
    )
    assert split.status_code == 303
    assert len(_lines(db_session, cid)) == 3


def test_review_shows_merge_control_only_when_flag_on(client, db_session, fake_ocr, tmp_path):
    _svc, _repos, claim = _claim_with_lines(client, db_session, fake_ocr, tmp_path, n=2)
    assert 'id="merge-btn"' not in client.get(f"/claims/{claim.id}/review").text
    _enable_split(db_session)
    assert 'id="merge-btn"' in client.get(f"/claims/{claim.id}/review").text
