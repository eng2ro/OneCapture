"""Claim document-header (grouping) band: render + edit (POST /claims/{id}/header).

A claim is a document header that groups N line items. The header carries the
fields the approver and the ERP read first — posting date, purpose (business
justification, posts to ERP), remarks (internal note), cost dimensions. These
are edited as a unit from the review screen, gated on claim status like line
edits.
"""

from __future__ import annotations

from decimal import Decimal

from eclaim.db.models import Claim
from eclaim.ocr.base import Extraction

_DIESEL = Extraction(expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")


def _upload(client, fake_ocr, extraction: Extraction = _DIESEL) -> str:
    fake_ocr.extraction = extraction
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    return client.post("/api/claims/upload", files=files).json()["id"]


def _reload(db_session, cid: str) -> Claim:
    import uuid
    db_session.expire_all()   # drop stale identity-map state from before the POST
    return db_session.get(Claim, uuid.UUID(cid))


def test_review_renders_document_header_band(client, fake_ocr):
    cid = _upload(client, fake_ocr)
    page = client.get(f"/claims/{cid}/review").text
    assert "Claim header" in page          # the grouping band title
    assert "Posting date" in page
    assert "Remarks" in page
    assert f'action="/claims/{cid}/header"' in page   # inline edit form wired


def test_edit_header_sets_grouping_fields(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr)
    r = client.post(
        f"/claims/{cid}/header",
        data={
            "purpose": "Regional sales enablement",
            "remarks": "split across two cards",
            "posting_date": "2026-06-30",
            "department": "SALES-02",
            "project_code": "T-1187",
            "claim_currency": "MYR",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    claim = _reload(db_session, cid)
    assert claim.purpose == "Regional sales enablement"
    assert claim.remarks == "split across two cards"
    assert str(claim.posting_date) == "2026-06-30"
    assert claim.department == "SALES-02"
    assert claim.project_code == "T-1187"

    # The edit is in the audit trail with the touched header fields.
    events = client.get(f"/api/audit/{cid}").json()
    edited = next(e for e in events if e["event_type"] == "edited")
    assert "posting_date" in edited["detail"]["header_fields"]
    assert "remarks" in edited["detail"]["header_fields"]
    # ...and the old->new value of each change, so a dispute can answer "who changed
    # this to what", not merely "which field was touched".
    changes = edited["detail"]["changes"]
    assert changes["remarks"]["to"] == "split across two cards"
    assert changes["posting_date"]["to"] == "2026-06-30"
    assert changes["department"] == {"from": None, "to": "SALES-02"}


def test_edit_header_blank_clears_a_field(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr)
    client.post(f"/claims/{cid}/header", data={"remarks": "temporary"},
                follow_redirects=False)
    assert _reload(db_session, cid).remarks == "temporary"
    # Re-submitting the field empty clears it (stored as NULL, not "").
    client.post(f"/claims/{cid}/header", data={"remarks": ""}, follow_redirects=False)
    assert _reload(db_session, cid).remarks is None


def test_edit_header_refused_once_decided(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr)
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    # An approved claim's header is locked — unapprove → amend, not silent edit.
    client.post(f"/claims/{cid}/header", data={"remarks": "too late"},
                follow_redirects=False)
    assert _reload(db_session, cid).remarks is None
