"""Submitter verification (setting capture.submitter_verification, owner
request 2026-07-08): the UPLOADER verifies the captured content and the
claim-vs-vendor-bill routing before the transaction reaches the approver —
only a verified transaction goes to the next step. Off by default (no
behaviour change until a company enables it).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from eclaim.db.models import Claim
from eclaim.ocr.base import Extraction
from eclaim.services import settings as settings_service


def _enable(db_session):
    ids = db_session.info["principal"]
    settings_service.set_setting(
        db_session, firm_id=ids["firm"], client_id=ids["client"],
        key="capture.submitter_verification", value="on", actor="t",
    )
    db_session.commit()


def _upload(client, fake_ocr, marker=b"sv"):
    fake_ocr.extraction = Extraction(
        vendor="Kedai V", total_amount=Decimal("50.00"), expense_type="other",
    )
    files = {"file": ("r.png", b"\x89PNG " + marker, "image/png")}
    return client.post("/api/claims/upload", files=files,
                       data={"attested": "true"}).json()["id"]


def test_capture_parks_at_submitted_until_the_uploader_confirms(client, fake_ocr, db_session):
    _enable(db_session)
    cid = _upload(client, fake_ocr)
    claim = db_session.get(Claim, uuid.UUID(cid))
    assert claim.status == "submitted"                   # parked, NOT in review

    # The approver cannot touch it before verification.
    assert client.post(f"/api/claims/{cid}/approve").status_code in (400, 409)

    # The review page shows the uploader's verification banner + confirm button.
    page = client.get(f"/claims/{cid}/review").text
    assert "waiting for YOUR verification" in page
    assert f"/claims/{cid}/confirm" in page

    # Confirm → in_review, audited, and now approval works.
    r = client.post(f"/claims/{cid}/confirm", follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Claim, uuid.UUID(cid)).status == "in_review"
    events = client.get(f"/api/audit/{cid}").json()
    assert any(e["event_type"] == "content_verified" for e in events)
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200


def test_confirm_refused_when_not_parked_and_is_idempotent_safe(client, fake_ocr, db_session):
    _enable(db_session)
    cid = _upload(client, fake_ocr, marker=b"sv2")
    assert client.post(f"/claims/{cid}/confirm", follow_redirects=False).status_code == 303
    # Second confirm: already in_review → refused (no silent double transition).
    r = client.post(f"/claims/{cid}/confirm", follow_redirects=False)
    assert r.status_code == 200 and "cannot confirm" in r.text


def test_parked_claims_stay_out_of_the_approvals_inbox(client, fake_ocr, db_session):
    _enable(db_session)
    cid = _upload(client, fake_ocr, marker=b"sv3")
    page = client.get("/approvals")
    assert f"/claims/{cid}/review" not in page.text      # uploader's todo, not approver's
    client.post(f"/claims/{cid}/confirm", follow_redirects=False)
    page = client.get("/approvals")
    assert f"/claims/{cid}/review" in page.text          # verified → approver's queue


def test_mileage_skips_the_park(client, db_session):
    _enable(db_session)
    r = client.post("/capture/mileage", data={
        "origin": "KL", "destination": "Seremban", "trip_date": "2026-07-05",
        "attested": "yes", "vehicle_id": "",
    }, follow_redirects=False)
    cid = r.headers["location"].split("/claims/")[1].split("/")[0]
    assert db_session.get(Claim, uuid.UUID(cid)).status == "in_review"   # no OCR to verify


def test_setting_off_keeps_captures_going_straight_to_review(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr, marker=b"sv4")       # default: off
    assert db_session.get(Claim, uuid.UUID(cid)).status == "in_review"
