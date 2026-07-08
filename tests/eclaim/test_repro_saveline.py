"""Save line keeps the reviewer's place (live bug report, 2026-07-08).

The verify modal is a client-side viewer paging "1 of N"; Save line posts and
reloads the page, which CLOSED the modal — to the user, their work "disappeared"
after every save. The fix: the redirect carries ?open=<line id> and the page
reopens the modal on that line. This test posts the exact field set from the
reported receipt (KIDDIES KOTTAGE) and pins the save + the reopen redirect.
"""
import uuid
from decimal import Decimal

from sqlalchemy import select

from eclaim.db.models import ClaimLine
from eclaim.ocr.base import Extraction


def _upload(client, fake_ocr):
    fake_ocr.extraction = Extraction(
        vendor="KIDDIES KOTTAGE", doc_no="2024/00593",
        total_amount=Decimal("39.60"), currency="RM", expense_type="other",
    )
    files = {"file": ("r.png", b"\x89PNG kk", "image/png")}
    return client.post("/api/claims/upload", files=files,
                       data={"attested": "true"}).json()["id"]


def test_save_line_saves_and_reopens_the_modal_on_that_line(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr)
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()

    # The verify modal posts EVERY rendered field, exactly as in the report.
    r = client.post(f"/claims/{cid}/edit", data={
        "line_id": str(line.id),
        "vendor": "KIDDIES KOTTAGE", "doc_no": "2024/00593",
        "doc_date": "", "total_amount": "39.60",
        "currency": "RM", "expense_type": "other",
        "quantity": "", "unit": "",
        "payment_method": "out_of_pocket", "business_reason": "",
        "gl_code": "6900", "cost_centre_override": "",
        "department": "", "project_code": "",
        "posting_date": "", "supplier_tax_id": "",
        "tax_amount": "", "tax_code": "",
        "tax_inclusive": "1", "fx_rate": "",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    # The redirect reopens the SAME line's modal — the reviewer keeps their place.
    assert r.headers["location"] == f"/claims/{cid}/review?open={line.id}"

    db_session.expire_all()
    lines_after = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().all()
    assert len(lines_after) == 1                         # nothing disappeared
    assert lines_after[0].gl_code == "6900"              # and the save landed


def test_review_page_carries_the_reopen_script(client, fake_ocr):
    cid = _upload(client, fake_ocr)
    page = client.get(f"/claims/{cid}/review")
    assert 'URLSearchParams' in page.text and '"open"' in page.text
