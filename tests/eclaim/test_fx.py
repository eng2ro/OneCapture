"""Exchange-rate module (Appendix G-C): monthly currency → MYR rates.

CarbonNext consumes MYR, so foreign lines convert: line fx_rate (human) wins →
table rate for the document month auto-prefills → none = flagged, release notes.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import select

from eclaim.db.models import CarbonHandoff, Category, Claim, ClaimLine
from eclaim.ocr.base import Extraction
from eclaim.services import fx
from eclaim.services.claims import ClaimService, Repos


def _seed_rate(db_session, ccy="USD", period=dt.date(2025, 9, 1), rate="4.70"):
    ids = db_session.info["principal"]
    return fx.upsert_rate(
        db_session, firm_id=ids["firm"], client_id=ids["client"],
        currency=ccy, period=period, rate_to_myr=Decimal(rate), actor="t",
    )


# --------------------------------------------------------------------------- #
# Service: lookup + upsert + delete
# --------------------------------------------------------------------------- #
def test_rate_for_matches_the_document_month_only(client, db_session):
    ids = db_session.info["principal"]
    _seed_rate(db_session, period=dt.date(2025, 9, 1), rate="4.70")

    assert fx.rate_for(db_session, ids["client"], "USD", dt.date(2025, 9, 26)) == Decimal("4.70")
    # no adjacent-month fallback: a missing month is entered, never guessed
    assert fx.rate_for(db_session, ids["client"], "USD", dt.date(2025, 10, 2)) is None
    # MYR (and unknown currency / no date) never converts
    assert fx.rate_for(db_session, ids["client"], "MYR", dt.date(2025, 9, 26)) is None
    assert fx.rate_for(db_session, ids["client"], None, dt.date(2025, 9, 26)) is None
    assert fx.rate_for(db_session, ids["client"], "USD", None) is None


def test_upsert_updates_in_place_and_audits_old_to_new(client, db_session):
    ids = db_session.info["principal"]
    row = _seed_rate(db_session, rate="4.70")
    row2 = _seed_rate(db_session, rate="4.75")           # same (ccy, month) → update
    assert row2.id == row.id
    assert row2.rate_to_myr == Decimal("4.75")

    chain = Repos.for_session(db_session).audit.chain("exchange_rate", row.id)
    events = [e.event_type for e in chain]
    assert events == ["fx_rate_added", "fx_rate_changed"]
    changed = chain[-1]
    assert changed.detail["from"] == "4.70" and changed.detail["to"] == "4.75"


def test_delete_rate_is_audited(client, db_session):
    db_session.info["principal"]
    row = _seed_rate(db_session)
    rid = row.id
    fx.delete_rate(db_session, rate_id=rid, actor="t")
    chain = Repos.for_session(db_session).audit.chain("exchange_rate", rid)
    assert any(e.event_type == "fx_rate_deleted" for e in chain)


# --------------------------------------------------------------------------- #
# Auto-prefill at capture and on edit; human rate wins
# --------------------------------------------------------------------------- #
def _cat(db_session):
    ids = db_session.info["principal"]
    return db_session.execute(
        select(Category).where(
            Category.client_id == ids["client"], Category.expense_type == "fuel_diesel"
        )
    ).scalar_one()


def _usd_claim(client, fake_ocr, *, date="26 SEP 2025", ccy="USD"):
    fake_ocr.extraction = Extraction(
        vendor="US Fuel Stop", total_amount=Decimal("100.00"), currency=ccy,
        date=date, expense_type="fuel_diesel", quantity=Decimal("30"), unit="L",
    )
    files = {"file": ("r.png", b"\x89PNG usd " + date.encode(), "image/png")}
    return client.post("/api/claims/upload", files=files,
                       data={"attested": "true"}).json()["id"]


def test_capture_prefills_fx_from_the_table_and_derives_myr_base(client, fake_ocr, db_session):
    _seed_rate(db_session, period=dt.date(2025, 9, 1), rate="4.70")
    cid = _usd_claim(client, fake_ocr)
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()
    assert line.fx_rate == Decimal("4.70")               # auto-prefilled
    assert line.base_amount == Decimal("470.00")         # 100 USD × 4.70


def test_no_rate_for_the_month_leaves_fx_empty_and_review_warns(client, fake_ocr, db_session):
    cid = _usd_claim(client, fake_ocr)                   # no table rate seeded
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()
    assert line.fx_rate is None
    page = client.get(f"/claims/{cid}/review")
    assert "Needs FX" in page.text


def test_human_entered_rate_wins_over_the_table(client, fake_ocr, db_session):
    _seed_rate(db_session, period=dt.date(2025, 9, 1), rate="4.70")
    cid = _usd_claim(client, fake_ocr)
    client.post(f"/claims/{cid}/edit",
                data={"line_id": "", "fx_rate": "4.85"}, follow_redirects=False)
    db_session.expire_all()
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()
    assert line.fx_rate == Decimal("4.85")               # the human's rate
    assert line.base_amount == Decimal("485.00")


def test_currency_correction_relooks_up_the_rate(client, fake_ocr, db_session):
    """OCR misread SGD as USD: the reviewer corrects the currency and the fx
    default re-resolves for the NEW currency (an explicit fx in the same edit
    would win instead)."""
    _seed_rate(db_session, ccy="USD", period=dt.date(2025, 9, 1), rate="4.70")
    _seed_rate(db_session, ccy="SGD", period=dt.date(2025, 9, 1), rate="3.45")
    cid = _usd_claim(client, fake_ocr)
    client.post(f"/claims/{cid}/edit",
                data={"line_id": "", "currency": "SGD"}, follow_redirects=False)
    db_session.expire_all()
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()
    assert line.currency == "SGD"
    assert line.fx_rate == Decimal("3.45")
    assert line.base_amount == Decimal("345.00")


# --------------------------------------------------------------------------- #
# Release notes unconverted foreign lines; the converted base reaches the handoff
# --------------------------------------------------------------------------- #
def test_unconverted_foreign_claim_cannot_be_approved_until_fx_set(client, fake_ocr, db_session):
    """A foreign line with no exchange rate has no MYR value, so the RM total and
    the authority/matrix gate would be incomplete. Approval is blocked until the
    rate is set (previously it silently approved on a currency-blind total — the
    fail-open the audit closed). Once the rate resolves, approve + release work and
    the converted base reaches the handoff."""
    cid = _usd_claim(client, fake_ocr)                   # no rate → unconverted
    r = client.post(f"/api/claims/{cid}/approve")
    assert r.status_code == 400 and "exchange rate" in r.text.lower()

    _seed_rate(db_session, ccy="USD", period=dt.date(2025, 9, 1), rate="4.70")
    db_session.commit()
    # Re-resolve FX on the line (currency edit re-runs the rate lookup).
    client.post(f"/claims/{cid}/edit",
                data={"line_id": "", "currency": "USD"}, follow_redirects=False)

    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    assert client.post(f"/api/claims/{cid}/release").status_code == 200
    handoff = db_session.query(CarbonHandoff).filter_by(
        claim_id=uuid.UUID(cid), direction="forward"
    ).one()
    assert handoff.currency == "USD" and handoff.base_amount == Decimal("470.00")


def test_prefilled_fx_flows_to_the_handoff_base(client, fake_ocr, db_session):
    _seed_rate(db_session, period=dt.date(2025, 9, 1), rate="4.70")
    cid = _usd_claim(client, fake_ocr)
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    assert client.post(f"/api/claims/{cid}/release").status_code == 200
    handoff = db_session.query(CarbonHandoff).filter_by(
        claim_id=uuid.UUID(cid), direction="forward"
    ).one()
    assert handoff.base_amount == Decimal("470.00")      # MYR figure CarbonNext consumes
    events = client.get(f"/api/audit/{cid}").json()
    released = next(e for e in events if e["event_type"] == "released")
    assert "fx_missing_lines" not in (released["detail"] or {})


# --------------------------------------------------------------------------- #
# Admin page
# --------------------------------------------------------------------------- #
def test_blanking_clears_optional_numerics(client, fake_ocr, db_session):
    """F-E item 6: an OCR-hallucinated quantity/tax/fx could be overtyped but never
    REMOVED — the verify form's inputs are always rendered prefilled, so blanking
    one is an explicit clear."""
    fake_ocr.extraction = Extraction(
        vendor="Kedai A", total_amount=Decimal("50.00"), currency="MYR",
        expense_type="fuel_diesel", quantity=Decimal("999"), unit="L",
        tax_amount=Decimal("3.00"),
    )
    files = {"file": ("r.png", b"\x89PNG clearme", "image/png")}
    cid = client.post("/api/claims/upload", files=files,
                      data={"attested": "true"}).json()["id"]
    client.post(f"/claims/{cid}/edit",
                data={"line_id": "", "quantity": "", "unit": "", "tax_amount": "",
                      "total_amount": "50.00"},
                follow_redirects=False)
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()
    assert line.quantity is None and line.unit is None and line.tax_amount is None
    assert line.total_amount == Decimal("50.00")         # gross never clearable
    assert line.net_amount == Decimal("50.00")           # re-derived without tax


def test_release_notes_category_missing_carbon_lines(client, fake_ocr, db_session):
    """F-E item 7: a carbon-relevant line forwarded with category NULL leaves
    CarbonNext only the raw expense_type — the release event must note it."""
    fake_ocr.extraction = Extraction(
        vendor="Unknown Shop", total_amount=Decimal("80.00"),
        expense_type="other", quantity=None,
    )
    files = {"file": ("r.png", b"\x89PNG nocat", "image/png")}
    cid = client.post("/api/claims/upload", files=files,
                      data={"attested": "true"}).json()["id"]
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()
    line.category_id = None                              # ensure unmapped
    db_session.commit()

    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    assert client.post(f"/api/claims/{cid}/release").status_code == 200
    events = client.get(f"/api/audit/{cid}").json()
    released = next(e for e in events if e["event_type"] == "released")
    assert released["detail"]["category_missing_lines"] == [1]


def test_admin_rates_page_crud(client, db_session):
    ids = db_session.info["principal"]
    r = client.post("/admin/rates", data={
        "client_id": str(ids["client"]), "currency": "usd",
        "period": "2025-09", "rate_to_myr": "4.70",
    }, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/admin/rates")
    assert "USD" in page.text and "4.7" in page.text

    # junk month / MYR / non-positive rate are rejected with a friendly error
    bad = client.post("/admin/rates", data={
        "client_id": str(ids["client"]), "currency": "MYR",
        "period": "2025-09", "rate_to_myr": "1",
    })
    assert "ISO code" in bad.text

    rate_row = fx.list_rates(db_session, [ids["client"]])[0]
    assert client.post("/admin/rates/delete", data={"rate_id": str(rate_row.id)},
                       follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert fx.list_rates(db_session, [ids["client"]]) == []
