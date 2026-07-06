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
