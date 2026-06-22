"""Evidence pack — thorough on assembly, smoke test on the PDF render.

The assembly (EvidenceService.build) is deterministic stored-data gathering, so
it gets the real coverage: full field set, ordered hash-linked trail, release
batch hash/TSA, RLS scoping, and regeneration equality. The PDF endpoint is only
checked for a valid, non-empty application/pdf response.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from eclaim.db.models import Claim, Claimant, Client, Firm
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimNotFound, Repos
from eclaim.services.evidence import EvidenceService


def _upload(client, fake_ocr, extraction: Extraction):
    fake_ocr.extraction = extraction
    files = {"file": ("receipt.png", b"\x89PNG\r\n fake-bytes", "image/png")}
    return client.post("/api/claims/upload", files=files)


def _release(client, fake_ocr, extraction: Extraction) -> str:
    cid = _upload(client, fake_ocr, extraction).json()["id"]
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    assert client.post(f"/api/claims/{cid}/release").status_code == 200
    return cid


# 1 -------------------------------------------------------------------------
def test_assembly_released_claim_full(client, fake_ocr, db_session):
    ids = db_session.info["principal"]
    claimant = Claimant(
        firm_id=ids["firm"], client_id=ids["client"], name="Alice",
        phone="+60123456", employee_ref="E-7", cost_centre="CC-42",
    )
    db_session.add(claimant)
    db_session.flush()

    cid = _release(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L",
        vendor="Shell", doc_no="INV-9", date="2026-02-02",
        currency="MYR", total_amount=Decimal("2000")))
    # Attach the claimant (the API upload channel leaves it null).
    claim = db_session.get(Claim, uuid.UUID(cid))
    claim.submitted_by_claimant_id = claimant.id
    db_session.flush()

    ev = EvidenceService.build(Repos.for_session(db_session), uuid.UUID(cid))

    # Header + confirmed fields
    assert ev.status == "released" and ev.released
    assert ev.vendor == "Shell" and ev.doc_no == "INV-9" and ev.doc_date == "2026-02-02"
    assert ev.currency == "MYR" and ev.total_amount == Decimal("2000.00")
    assert ev.quantity == Decimal("450.0000") and ev.unit == "L"
    assert ev.category_name == "Diesel"        # the seeded fuel_diesel category
    assert ev.scope == 1 and ev.factor_key == "fuel_diesel" and ev.factor_version == 1
    assert ev.tco2e == Decimal("1.206000") and ev.data_quality == "Activity-based"
    # Claimant
    assert (ev.claimant_name, ev.employee_ref, ev.cost_centre) == ("Alice", "E-7", "CC-42")
    # Approval trail — ordered + hash-linked
    assert [e.event_type for e in ev.trail] == ["submitted", "approved", "released", "tsa_anchored"]
    assert ev.trail[0].prev_hash in (None, "")
    for prev, cur in zip(ev.trail, ev.trail[1:]):
        assert cur.prev_hash == prev.hash
    # Integrity
    assert ev.batch_hash and len(ev.batch_hash) == 64
    assert ev.tsa_token.startswith("STUB-TSA:")


# 2 -------------------------------------------------------------------------
def test_assembly_in_review_claim(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="electricity", quantity=Decimal("12000"), unit="kWh")).json()["id"]

    ev = EvidenceService.build(Repos.for_session(db_session), uuid.UUID(cid))

    assert ev.status == "in_review" and not ev.released
    assert ev.batch_hash is None and ev.tsa_token is None
    assert [e.event_type for e in ev.trail] == ["submitted"]
    assert ev.scope == 2 and ev.factor_key == "electricity"
    assert (ev.claimant_name, ev.category_name) == (None, "Grid electricity")


# 3 -------------------------------------------------------------------------
def test_assembly_rls_blocks_other_firm(db_session, db_engine):
    """A claim in another firm is invisible to this principal — build raises
    ClaimNotFound (RLS returns no row), it does not leak the pack."""
    owner = Session(bind=db_engine, future=True, expire_on_commit=False)
    firm_b = None
    try:
        firm_b = Firm(name="Evidence RLS Firm B")
        owner.add(firm_b)
        owner.flush()
        client_b = Client(firm_id=firm_b.id, name="B Co", currency="MYR")
        owner.add(client_b)
        owner.flush()
        other = Claim(
            firm_id=firm_b.id, client_id=client_b.id, image_path="/o.png",
            image_sha256="o", status="released", currency="MYR",
        )
        owner.add(other)
        owner.flush()
        owner.commit()

        repos = Repos.for_session(db_session)  # firm A context
        with pytest.raises(ClaimNotFound):
            EvidenceService.build(repos, other.id)
    finally:
        if firm_b is not None:
            owner.execute(text("DELETE FROM claim WHERE firm_id = :f"), {"f": firm_b.id})
            owner.execute(text("DELETE FROM client WHERE firm_id = :f"), {"f": firm_b.id})
            owner.execute(text("DELETE FROM firm WHERE id = :f"), {"f": firm_b.id})
            owner.commit()
        owner.close()


# 4 -------------------------------------------------------------------------
def test_regenerate_yields_same_content(client, fake_ocr, db_session):
    cid = _release(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L"))
    repos = Repos.for_session(db_session)

    first = EvidenceService.build(repos, uuid.UUID(cid))
    second = EvidenceService.build(repos, uuid.UUID(cid))
    assert first == second  # deterministic — no generated-at inside the model


# 5 -------------------------------------------------------------------------
def test_evidence_endpoint_returns_pdf(client, fake_ocr):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]

    resp = client.get(f"/api/claims/{cid}/evidence")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content[:5] == b"%PDF-"   # a real PDF
    assert len(resp.content) > 500
