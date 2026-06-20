"""End-to-end e-Claim tests (spec §10), against a Postgres test DB; OCR mocked.

Covers: upload+classify, release (one entry/batch + linked audit chain),
idempotent re-release, immutability + reversing correction, ledger scope totals,
and deterministic batch hash recomputed from stored rows.
"""

from __future__ import annotations

from decimal import Decimal

from core.release import canonical_hash
from eclaim.db.models import Claim, EmissionEntry
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimService


def _upload(client, fake_ocr, extraction: Extraction):
    fake_ocr.extraction = extraction
    files = {"file": ("receipt.png", b"\x89PNG\r\n fake-bytes", "image/png")}
    return client.post("/api/claims/upload", files=files)


def _release(client, claim_id):
    assert client.post(f"/api/claims/{claim_id}/approve").status_code == 200
    return client.post(f"/api/claims/{claim_id}/release")


# 1 -------------------------------------------------------------------------
def test_upload_classifies_activity_and_spend(client, fake_ocr):
    diesel = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L",
        total_amount=Decimal("2000"))).json()
    assert diesel["basis"] == "activity"
    assert diesel["scope"] == 1
    assert diesel["tco2e"] == "1.206000"

    elec = _upload(client, fake_ocr, Extraction(
        expense_type="electricity", quantity=Decimal("12000"), unit="kWh")).json()
    assert elec["scope"] == 2
    assert elec["tco2e"] == "7.020000"

    spend = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=None, total_amount=Decimal("500"))).json()
    assert spend["basis"] == "spend"
    assert spend["tco2e"] == "0.175000"  # 500 * 0.35 / 1000
    assert "lower data quality" in spend["data_quality"]


# 2 -------------------------------------------------------------------------
def test_release_writes_one_entry_and_linked_audit_chain(client, fake_ocr):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]
    batch = _release(client, cid).json()
    assert batch["record_count"] == 1
    assert batch["tsa_token"].startswith("STUB-TSA:")

    ledger = client.get("/api/ledger").json()
    assert len(ledger["entries"]) == 1

    events = client.get(f"/api/audit/{cid}").json()
    types = [e["event_type"] for e in events]
    assert types == ["submitted", "approved", "released", "tsa_anchored"]
    # The chain links: each prev_hash equals the previous event's hash.
    assert events[0]["prev_hash"] in (None, "")
    for prev, cur in zip(events, events[1:]):
        assert cur["prev_hash"] == prev["hash"]


# 3 -------------------------------------------------------------------------
def test_re_release_is_idempotent(client, fake_ocr):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="electricity", quantity=Decimal("12000"), unit="kWh")).json()["id"]
    first = _release(client, cid).json()
    second = client.post(f"/api/claims/{cid}/release").json()
    assert first["batch_hash"] == second["batch_hash"]
    assert len(client.get("/api/ledger").json()["entries"]) == 1


# 4 -------------------------------------------------------------------------
def test_released_claim_is_immutable_and_corrected_by_reversal(client, fake_ocr):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]
    _release(client, cid)

    # No in-place edit of a released claim.
    assert client.patch(f"/api/claims/{cid}", json={"vendor": "X"}).status_code == 409
    # No delete capability at all.
    assert client.delete(f"/api/claims/{cid}").status_code == 405

    # Correction = a reversing entry (negative tCO2e), original untouched.
    rev = client.post(f"/api/claims/{cid}/reverse").json()
    assert Decimal(rev["tco2e"]) == Decimal("-1.206000")
    entries = client.get("/api/ledger").json()["entries"]
    assert len(entries) == 2
    assert sum(Decimal(e["tco2e"]) for e in entries) == Decimal("0.000000")


# 5 -------------------------------------------------------------------------
def test_ledger_scope_totals_equal_entry_sum(client, fake_ocr):
    specs = [
        Extraction(expense_type="fuel_diesel", quantity=Decimal("450"), unit="L"),    # S1
        Extraction(expense_type="electricity", quantity=Decimal("12000"), unit="kWh"),  # S2
        Extraction(expense_type="air_travel", quantity=Decimal("1000"), unit="km"),   # S3
    ]
    for ex in specs:
        cid = _upload(client, fake_ocr, ex).json()["id"]
        _release(client, cid)

    ledger = client.get("/api/ledger").json()
    entry_sum = sum(Decimal(e["tco2e"]) for e in ledger["entries"])
    assert Decimal(ledger["scope_1"]) == Decimal("1.206000")
    assert Decimal(ledger["scope_2"]) == Decimal("7.020000")
    assert Decimal(ledger["scope_3"]) == Decimal("0.180000")
    assert Decimal(ledger["total_tco2e"]) == entry_sum


# 6 -------------------------------------------------------------------------
def test_batch_hash_recomputes_from_stored_rows(client, fake_ocr, db_session):
    cid = _upload(client, fake_ocr, Extraction(
        expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")).json()["id"]
    batch = _release(client, cid).json()

    claim = db_session.get(Claim, cid)
    recomputed = canonical_hash([ClaimService._projection(claim)])
    assert recomputed == batch["batch_hash"]

    entry = db_session.query(EmissionEntry).filter_by(source_id=claim.id).one()
    assert entry.carbon_ref == f"CARB-{batch['batch_hash'][:12].upper()}"
