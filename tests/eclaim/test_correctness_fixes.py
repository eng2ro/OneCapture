"""Correctness fixes from the head-to-tail audit:
- net/base are derived at capture (not only on a later edit) → ERP export not blank
- partially_approved claims are releasable (the approved portion forwards/exports)
- release is idempotent even for a claim with no carbon-relevant lines
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

from sqlalchemy import select

from eclaim.auth.principal import Principal
from eclaim.db.models import Category, Claim, ClaimLine
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimService, Repos


def _partner(db_session) -> Principal:
    ids = db_session.info["principal"]
    return Principal(
        user_id=ids["user"], firm_id=ids["firm"], base_role="partner",
        allowed_client_ids=frozenset({ids["client"]}), email="partner@seed.test",
    )


def _lines(db_session, claim_id):
    return db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == claim_id).order_by(ClaimLine.line_no)
    ).scalars().all()


def _attest(db_session, claim):
    """Stamp the out-of-pocket attestation so the claim clears the release gate
    (P3) — the service-built equivalent of ticking the capture checkbox."""
    import datetime as dt

    claim.attested_by = "claimant@seed.test"
    claim.attested_at = dt.datetime.now(dt.timezone.utc)
    db_session.flush()


def _add_line(svc, repos, claim, fake_ocr, tmp_path, *, category_id=None, expense_type="fuel_diesel",
              total=Decimal("100")):
    fake_ocr.extraction = Extraction(expense_type=expense_type, total_amount=total,
                                     quantity=Decimal("10"), unit="L")
    return svc.add_line(
        repos=repos, claim=claim, image_bytes=b"\x89PNG\r\n fake", media_type="image/png",
        ocr=fake_ocr, image_dir=tmp_path, category_id=category_id,
    )


# --- human-readable claim number (migration 0016) ---------------------------
def test_claim_gets_human_readable_number(client, fake_ocr):
    out = client.post(
        "/api/claims/upload", files={"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    ).json()
    assert re.match(r"^CLM-\d{4}-\d{6}$", out["claim_no"]), out.get("claim_no")


# --- net/base derived at capture --------------------------------------------
def test_capture_derives_net_and_base(client, fake_ocr, db_session):
    fake_ocr.extraction = Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L",
        total_amount=Decimal("100.00"),
    )
    cid = client.post(
        "/api/claims/upload", files={"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    ).json()["id"]
    line = _lines(db_session, uuid.UUID(cid))[0]
    # Tax-inclusive by default with no tax/FX → net == base == gross, computed now.
    assert line.net_amount == Decimal("100.00")
    assert line.base_amount == Decimal("100.00")


# --- partially_approved is releasable ---------------------------------------
def test_partially_approved_claim_can_release(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    # API-style claim (created_by null) so the partner may decide it.
    claim = svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])
    l1 = _add_line(svc, repos, claim, fake_ocr, tmp_path)   # carbon (fuel_diesel)
    l2 = _add_line(svc, repos, claim, fake_ocr, tmp_path)
    _attest(db_session, claim)

    svc.decide(
        repos=repos, claim_id=claim.id, reviewer=_partner(db_session),
        decisions={l1.id: ("approved", None), l2.id: ("rejected", "duplicate")},
        actor="reviewer",
    )
    assert db_session.get(Claim, claim.id).status == "partially_approved"

    batch = svc.release(repos=repos, claim_id=claim.id, actor="reviewer", principal=_partner(db_session))
    assert db_session.get(Claim, claim.id).status == "released"
    assert batch.record_count == 1   # only the approved carbon line forwarded


# --- release idempotent with no carbon-relevant lines -----------------------
def test_release_idempotent_for_zero_carbon_claim(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    noncarbon = Category(
        firm_id=ids["firm"], client_id=ids["client"], name="Stationery",
        expense_type="other", carbon_relevant=False,
    )
    db_session.add(noncarbon)
    db_session.flush()

    claim = svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])
    _add_line(svc, repos, claim, fake_ocr, tmp_path, category_id=noncarbon.id, expense_type="other")
    _attest(db_session, claim)
    svc.approve(repos=repos, claim_id=claim.id, actor="reviewer", approver=_partner(db_session))

    first = svc.release(repos=repos, claim_id=claim.id, actor="reviewer", principal=_partner(db_session))
    assert first.record_count == 0   # nothing carbon-relevant to forward
    # Second release is an idempotent no-op returning the SAME batch (was an error).
    second = svc.release(repos=repos, claim_id=claim.id, actor="reviewer", principal=_partner(db_session))
    assert second.id == first.id
