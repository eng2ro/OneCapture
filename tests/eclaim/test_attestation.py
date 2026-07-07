"""Out-of-pocket attestation (Appendix A, Layer 1).

The web capture form must carry the declaration to submit; the claim then records
who attested and when, and that flows into the evidence pack.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from eclaim.auth.principal import Principal
from eclaim.db.models import Claim
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimError, ClaimService, Repos
from eclaim.services.evidence import EvidenceService
from eclaim.services.evidence_pdf import render as render_evidence_pdf
from eclaim.services.sod import SoDViolation
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


# --------------------------------------------------------------------------- #
# R2 — after-the-fact re-attest path for pre-P3 (NULL-attestation) claims
# --------------------------------------------------------------------------- #
def test_reattest_unblocks_a_stuck_out_of_pocket_claim(client):
    """The core R2 fix: an out-of-pocket claim uploaded WITHOUT attestation is blocked
    at release (400), then the re-attest action lets it through. Pins the whole path —
    if ``attest`` stops stamping, the second release stays blocked and this fails."""
    cid = _upload_receipt(client, attested=False)
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    assert client.post(f"/api/claims/{cid}/release").status_code == 400   # stuck

    assert client.post(f"/api/claims/{cid}/attest").status_code == 200
    assert client.post(f"/api/claims/{cid}/release").status_code == 200    # now clears
    assert client.get(f"/api/claims/{cid}").json()["status"] == "released"


def test_attest_stamps_and_writes_an_audit_event(client, db_session):
    """Attesting records who + when on the claim and appends a durable ``attested``
    audit event — the evidence the control exists to capture."""
    cid = _upload_receipt(client, attested=False)
    assert db_session.get(Claim, cid).attested_by is None

    client.post(f"/api/claims/{cid}/attest")

    claim = db_session.get(Claim, cid)
    # Attributed to the API actor (``system`` in the test harness, the caller's email
    # on the web path); the point is the stamp is now recorded, not who exactly.
    assert claim.attested_by is not None
    assert claim.attested_at is not None
    chain = Repos.for_session(db_session).audit.chain("claim", cid)
    assert any(e.event_type == "attested" for e in chain)


def test_cannot_reattest_an_already_attested_claim(client):
    """An already-attested claim is a 409 — the original attester/timestamp is evidence
    and is never silently overwritten by a second attestation."""
    cid = _upload_receipt(client, attested=True)          # attested at upload
    r = client.post(f"/api/claims/{cid}/attest")
    assert r.status_code == 409
    assert "already attested" in r.json()["detail"].lower()


def test_cannot_attest_after_release(client):
    """Attestation must precede release: once released, the claim is locked (409)."""
    cid = _upload_receipt(client, attested=True)
    client.post(f"/api/claims/{cid}/approve")
    assert client.post(f"/api/claims/{cid}/release").status_code == 200
    assert client.post(f"/api/claims/{cid}/attest").status_code == 409


def test_web_review_offers_reattest_and_hides_it_once_done(client, db_session):
    """The review screen offers the after-the-fact attestation for an unattested
    out-of-pocket claim, the web action stamps it, and the affordance disappears once
    attested — pins the ``can_attest`` gate in both directions."""
    cid = _upload_receipt(client, attested=False)
    assert "Attest out-of-pocket" in client.get(f"/claims/{cid}/review").text

    r = client.post(f"/claims/{cid}/attest", follow_redirects=False)
    assert r.status_code == 303
    assert db_session.get(Claim, cid).attested_by == "partner@seed.test"   # web actor
    assert "Attest out-of-pocket" not in client.get(f"/claims/{cid}/review").text


def _out_of_pocket_claim(svc, repos, fake_ocr, tmp_path, ids, *, payment_method):
    claim = svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])
    fake_ocr.extraction = Extraction(expense_type="other", total_amount=Decimal("100"))
    line = svc.add_line(
        repos=repos, claim=claim, image_bytes=b"\x89PNG img", media_type="image/png",
        ocr=fake_ocr, image_dir=tmp_path,
    )
    if payment_method != "out_of_pocket":
        svc.edit(
            repos=repos, claim_id=claim.id, line_id=line.id, actor="seed",
            fields={"payment_method": payment_method},
        )
    return claim


def test_attest_requires_out_of_pocket_spend(client, fake_ocr, db_session, tmp_path):
    """There is nothing to attest on a claim with no out-of-pocket lines — a
    corporate-card claim rejects the attestation rather than stamping a meaningless
    declaration."""
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    claim = _out_of_pocket_claim(
        svc, repos, fake_ocr, tmp_path, ids, payment_method="corporate_card"
    )
    with pytest.raises(ClaimError, match="nothing to attest"):
        svc.attest(repos=repos, claim_id=claim.id, actor="seed")


def test_viewer_cannot_attest(client, fake_ocr, db_session, tmp_path):
    """A Viewer may never mutate a claim — attestation included."""
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    claim = _out_of_pocket_claim(
        svc, repos, fake_ocr, tmp_path, ids, payment_method="out_of_pocket"
    )
    viewer = Principal(
        user_id=ids["user"], firm_id=ids["firm"], base_role="viewer",
        allowed_client_ids=frozenset({ids["client"]}), email="viewer@seed.test",
    )
    with pytest.raises(SoDViolation):
        svc.attest(repos=repos, claim_id=claim.id, actor="viewer", principal=viewer)


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
