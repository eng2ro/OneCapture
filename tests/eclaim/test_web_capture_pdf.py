"""Capture of a PDF upload — DB-backed, fake OCR.

Policy ``allow_document_split`` (per client, off by default) decides whether a
multi-page PDF is kept whole (strict 1:1 provenance → one line) or split into one
line per page.
"""

from __future__ import annotations

import json
import re
import uuid

from fpdf import FPDF
from sqlalchemy import select

from eclaim.db.models import Client, ClaimLine


def _pdf_bytes(pages: list[str]) -> bytes:
    doc = FPDF()
    for text in pages:
        doc.add_page()
        doc.set_font("Helvetica", size=20)
        doc.cell(0, 20, text)
    return bytes(doc.output())


def _post_pdf(client, pages):
    return client.post(
        "/capture",
        files=[("files", ("invoices.pdf", _pdf_bytes(pages), "application/pdf"))],
        data={"items": json.dumps([None]), "attested": "yes"},   # no client-side fields — server reads it
        follow_redirects=False,
    )


def _claim_id(resp) -> str:
    m = re.match(r"^/claims/([0-9a-f-]+)/review$", resp.headers["location"])
    assert m, resp.headers.get("location")
    return m.group(1)


def _lines(db_session, claim_id):
    return db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == claim_id).order_by(ClaimLine.line_no)
    ).scalars().all()


def _enable_split(db_session):
    cid = db_session.info["principal"]["client"]
    cl = db_session.get(Client, cid)
    cl.modules = {**(cl.modules or {}), "allow_document_split": True}
    db_session.flush()


def test_pdf_kept_whole_as_one_line_by_default(client, fake_ocr, db_session):
    resp = _post_pdf(client, ["Invoice A", "Invoice B", "Invoice C"])
    assert resp.status_code == 303
    lines = _lines(db_session, uuid.UUID(_claim_id(resp)))
    assert len(lines) == 1   # strict 1:1 provenance — the whole PDF is one line


def test_pdf_splits_one_line_per_page_when_policy_on(client, fake_ocr, db_session):
    _enable_split(db_session)
    resp = _post_pdf(client, ["Invoice A", "Invoice B", "Invoice C"])
    assert resp.status_code == 303
    lines = _lines(db_session, uuid.UUID(_claim_id(resp)))
    assert len(lines) == 3   # one line per page


def test_unreadable_pdf_makes_no_line_and_does_not_crash(client, fake_ocr, db_session):
    resp = client.post(
        "/capture",
        files=[("files", ("broken.pdf", b"%PDF-1.4 not really", "application/pdf"))],
        data={"items": json.dumps([None]), "attested": "yes"},
        follow_redirects=False,
    )
    # A mileage-less, receipt-less claim still lands (its review screen shows the
    # error); the point is the bad PDF produced no line and no 500.
    assert resp.status_code in (303, 200)
    if resp.status_code == 303:
        assert _lines(db_session, uuid.UUID(_claim_id(resp))) == []
