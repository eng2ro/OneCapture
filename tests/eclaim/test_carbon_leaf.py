"""Appendix D — the carbon-related leaf icon renders on every listing.

OneCapture does not compute CO2e; it *differentiates* which transactions are
carbon-related (those post to CarbonNext on release). Each listing shows a green
leaf for a carbon-related row and a muted dash otherwise. The rendered markers are
``data-carbon="1"`` (leaf) and ``data-carbon="0"`` (dash).

One rendering test per screen. Each asserts BOTH the leaf (for a carbon-relevant
row) and the dash (for a non-carbon row), so the test fails if the column is
reverted/removed — mutation-proof, not just "renders without error".

* e-Claim claim list  — leaf when any line is ``carbon_relevant``
* e-Claim review lines — leaf per line ``carbon_relevant``
* ERP Sync queue       — leaf when ``category != 'UNMAPPED'``
* ERP Sync entry detail — same rule
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from eclaim.db.models import Category, ErpsyncEntry
from eclaim.ocr.base import Extraction
from erpsync.persistence.pg_staging import PgStagingStore
from erpsync.persistence.store import Store
from erpsync.pipeline import run_import
from gen_synthetic import month_rows, write_csv

LEAF = 'data-carbon="1"'
DASH = 'data-carbon="0"'

# Diesel auto-maps to a carbon-relevant category (leaf). For a non-carbon row we
# must MAP to a category flagged carbon_relevant=False — an *unmapped* line defaults
# to carbon-relevant (classify.carbon_relevant_for: None → True, fail-safe forward),
# so "other" alone would still be a leaf.
_DIESEL = Extraction(expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")
_OTHER = Extraction(expense_type="other", total_amount=Decimal("100"))


def _upload(client, fake_ocr, extraction: Extraction) -> str:
    fake_ocr.extraction = extraction
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    return client.post("/api/claims/upload", files=files).json()["id"]


def _noncarbon_category(db_session) -> Category:
    """A mapped-but-not-carbon category (e.g. bank charges) — the only way to get a
    line whose ``carbon_relevant`` is False, since unmapped defaults to True."""
    ids = db_session.info["principal"]
    cat = Category(
        firm_id=ids["firm"], client_id=ids["client"],
        name="Bank charges", expense_type="bank_charge", carbon_relevant=False,
    )
    db_session.add(cat)
    db_session.flush()
    return cat


def _make_noncarbon_claim(client, fake_ocr, db_session) -> str:
    """Upload a claim and reassign its line to a non-carbon category → a dash row."""
    cid = _upload(client, fake_ocr, _OTHER)
    cat = _noncarbon_category(db_session)
    client.post(f"/claims/{cid}/category", data={"category_id": str(cat.id)},
                follow_redirects=False)
    return cid


def _stage_month(db_session, config, tmp_path):
    ids = db_session.info["principal"]
    sink = PgStagingStore(db_session, firm_id=ids["firm"], client_id=ids["client"])
    listing = tmp_path / "month.csv"
    write_csv(listing, month_rows())
    run_import(listing, config, Store(), staging=sink)
    db_session.flush()
    return ids


# 1 — e-Claim claim list ----------------------------------------------------- #
def test_claims_list_shows_leaf_and_dash(client, fake_ocr, db_session):
    _upload(client, fake_ocr, _DIESEL)                 # carbon-relevant → leaf
    _make_noncarbon_claim(client, fake_ocr, db_session)  # mapped non-carbon → dash
    page = client.get("/claims")
    assert page.status_code == 200
    assert LEAF in page.text, "carbon-relevant claim shows no leaf"
    assert DASH in page.text, "non-carbon claim shows no dash"


# 2 — e-Claim review lines --------------------------------------------------- #
def test_review_lines_show_leaf_and_dash(client, fake_ocr, db_session):
    carbon = _upload(client, fake_ocr, _DIESEL)
    plain = _make_noncarbon_claim(client, fake_ocr, db_session)
    assert LEAF in client.get(f"/claims/{carbon}/review").text, "carbon line shows no leaf"
    assert DASH in client.get(f"/claims/{plain}/review").text, "non-carbon line shows no dash"


# 3 — ERP Sync review queue -------------------------------------------------- #
def test_erpsync_queue_shows_leaf_and_dash(client, db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    # The queue shows held + flagged rows. Pin one mapped (leaf) and one UNMAPPED
    # (dash) so the assertion is independent of the synthetic month's mix.
    held = db_session.execute(
        select(ErpsyncEntry).where(
            ErpsyncEntry.client_id == ids["client"], ErpsyncEntry.status == "held"
        ).limit(1)
    ).scalar_one()
    flagged = db_session.execute(
        select(ErpsyncEntry).where(
            ErpsyncEntry.client_id == ids["client"], ErpsyncEntry.status == "flagged"
        ).limit(1)
    ).scalar_one()
    held.category = "Purchased electricity"   # mapped → carbon-related → leaf
    flagged.category = "UNMAPPED"             # → dash
    db_session.flush()

    page = client.get("/erpsync/review")
    assert page.status_code == 200
    assert LEAF in page.text, "mapped staged row shows no leaf"
    assert DASH in page.text, "UNMAPPED staged row shows no dash"


# 4 — ERP Sync entry detail -------------------------------------------------- #
def test_erpsync_entry_shows_leaf_and_dash(client, db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    rows = db_session.execute(
        select(ErpsyncEntry).where(ErpsyncEntry.client_id == ids["client"]).limit(2)
    ).scalars().all()
    mapped, unmapped = rows[0], rows[1]
    mapped.category = "Purchased fuel"        # mapped → carbon-related → leaf
    unmapped.category = "UNMAPPED"            # → dash
    db_session.flush()

    assert LEAF in client.get(f"/erpsync/entries/{mapped.id}/review").text, \
        "mapped entry detail shows no leaf"
    assert DASH in client.get(f"/erpsync/entries/{unmapped.id}/review").text, \
        "UNMAPPED entry detail shows no dash"
