"""Out-of-pocket attestation (Appendix A, Layer 1).

The web capture form must carry the declaration to submit; the claim then records
who attested and when, and that flows into the evidence pack.
"""

from __future__ import annotations

from sqlalchemy import select

from eclaim.db.models import Claim
from eclaim.services.claims import Repos
from eclaim.services.evidence import EvidenceService
from eclaim.services.evidence_pdf import render as render_evidence_pdf
from datetime import datetime, timezone


def _files(n):
    return [("files", (f"r{i}.png", b"\x89PNG\r\n fake", "image/png")) for i in range(n)]


def test_capture_without_attestation_is_blocked(client, db_session):
    resp = client.post(
        "/capture", files=_files(1), data={"items": "[]"}, follow_redirects=False
    )
    assert resp.status_code == 200                       # re-render, not a redirect
    assert "out-of-pocket declaration" in resp.text
    assert db_session.execute(select(Claim)).scalars().first() is None   # nothing saved


def test_capture_with_attestation_stamps_the_claim(client, db_session):
    resp = client.post(
        "/capture", files=_files(1), data={"items": "[]", "attested": "yes"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    claim = db_session.execute(select(Claim)).scalars().one()
    assert claim.attested_by == "partner@seed.test"      # the capturing principal
    assert claim.attested_at is not None


def _upload_receipt(client, *, attested: bool):
    """Single out-of-pocket receipt via the JSON API, optionally attested."""
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    data = {"attested": "true"} if attested else None
    return client.post("/api/claims/upload", files=files, data=data).json()["id"]


def test_api_upload_records_attestation(client, db_session):
    """The JSON upload path records the attestation when the flag is set — parity with
    the web capture form (punch-list P3). Fails if the API drops the flag again."""
    cid = _upload_receipt(client, attested=True)
    claim = db_session.get(Claim, cid)
    # Recorded (attributed to the API actor); the point is the flag is no longer
    # dropped — an unattested upload leaves this NULL (see the release-gate test).
    assert claim.attested_by is not None
    assert claim.attested_at is not None


def test_release_blocked_for_unattested_out_of_pocket_claim(client):
    """The downstream gate: an out-of-pocket claim uploaded WITHOUT attestation can be
    approved but must NOT release — this is what closes the bypass on the JSON API and
    legacy mileage paths. Fails (release would 200) if the gate is removed."""
    cid = _upload_receipt(client, attested=False)
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    blocked = client.post(f"/api/claims/{cid}/release")
    assert blocked.status_code == 400
    assert "attestation" in blocked.json()["detail"].lower()
    assert client.get(f"/api/claims/{cid}").json()["status"] == "approved"   # not released


def test_release_allowed_once_attested(client):
    """The same flow with attestation on file releases cleanly — the gate blocks only
    the un-attested case, not out-of-pocket reimbursement per se."""
    cid = _upload_receipt(client, attested=True)
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    assert client.post(f"/api/claims/{cid}/release").status_code == 200
    assert client.get(f"/api/claims/{cid}").json()["status"] == "released"


def test_attestation_flows_into_the_evidence_pack(client, db_session):
    client.post(
        "/capture", files=_files(1), data={"items": "[]", "attested": "yes"},
        follow_redirects=False,
    )
    claim = db_session.execute(select(Claim)).scalars().one()
    ev = EvidenceService.build(Repos.for_session(db_session), claim.id)
    assert ev.attested_by == "partner@seed.test"
    assert ev.attested_at is not None
    pdf = render_evidence_pdf(ev, datetime.now(timezone.utc))
    assert pdf[:4] == b"%PDF" and len(pdf) > 500          # renders a real PDF
