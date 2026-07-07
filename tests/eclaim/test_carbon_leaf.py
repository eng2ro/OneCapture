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
* ERP Sync queue       — leaf when the row is carbon-mapped AND still posting
* ERP Sync entry detail — same rule, via carbon_leaf_state (R3)
"""

from __future__ import annotations

import re
from decimal import Decimal

from sqlalchemy import select

from eclaim.db.models import Category, ErpsyncEntry
from eclaim.ocr.base import Extraction
from erpsync.persistence.pg_staging import PgStagingStore
from erpsync.persistence.store import Store
from erpsync.pipeline import run_import
from erpsync.review.leaf import carbon_leaf_state
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


# --------------------------------------------------------------------------- #
# R3 — the leaf reconciles carbon-relatedness with releasability
# --------------------------------------------------------------------------- #
def test_leaf_state_clean_mapped_posts_on_release():
    s = carbon_leaf_state("clean", "Purchased fuel")
    assert s.carbon and not s.muted
    assert s.tooltip == "Carbon-related — posts to CarbonNext on release"


def test_leaf_state_pending_mapped_posts_once_approved():
    """A held/flagged mapped row is carbon-related but not yet releasable — it posts
    only AFTER approval, and the tooltip says so rather than claiming it already will."""
    s = carbon_leaf_state("held", "Purchased fuel")
    assert s.carbon
    assert "once approved" in s.tooltip


def test_leaf_state_dismissed_mapped_does_not_claim_posting():
    """R3: a dismissed but mapped row must NOT keep saying "posts on release" — it is
    carbon-related yet will never post. Pins the suppression of the false claim."""
    s = carbon_leaf_state("dismissed", "Purchased fuel")
    assert s.carbon and s.muted
    assert "posts to CarbonNext on release" not in s.tooltip
    assert "will not post" in s.tooltip


def test_leaf_state_approved_unmapped_discloses_it_still_posts():
    """R3: an approved-as-is UNMAPPED row IS released (posts to the ledger) but carries
    no carbon factor — the dash must disclose that, not read as "nothing happens"."""
    s = carbon_leaf_state("approved", "UNMAPPED")
    assert not s.carbon
    assert "posts to the ledger" in s.tooltip


def test_leaf_state_dismissed_unmapped_is_plain_not_related():
    """An UNMAPPED row that will NOT post gets the plain "not carbon-related" dash — no
    misleading ledger-posting note."""
    s = carbon_leaf_state("dismissed", "UNMAPPED")
    assert not s.carbon
    assert s.tooltip == "Not carbon-related"


def test_erpsync_entry_dismissed_mapped_makes_no_posting_promise(client, db_session, config, tmp_path):
    """End-to-end on the entry page: a dismissed mapped row still shows a leaf but no
    longer claims it "posts to CarbonNext on release" (the R3 bug)."""
    ids = _stage_month(db_session, config, tmp_path)
    row = db_session.execute(
        select(ErpsyncEntry).where(ErpsyncEntry.client_id == ids["client"]).limit(1)
    ).scalar_one()
    row.category = "Purchased fuel"
    row.status = "dismissed"
    db_session.flush()

    page = client.get(f"/erpsync/entries/{row.id}/review").text
    assert LEAF in page                                    # still carbon-related
    assert "posts to CarbonNext on release" not in page    # but no false promise
    assert "will not post" in page


def test_erpsync_entry_approved_unmapped_is_not_a_silent_dash(client, db_session, config, tmp_path):
    """End-to-end: an approved-as-is UNMAPPED row shows a dash that DISCLOSES it still
    posts to the ledger — no longer the silent bare dash the R3 bug described."""
    ids = _stage_month(db_session, config, tmp_path)
    row = db_session.execute(
        select(ErpsyncEntry).where(ErpsyncEntry.client_id == ids["client"]).limit(1)
    ).scalar_one()
    row.category = "UNMAPPED"
    row.status = "approved"
    db_session.flush()

    page = client.get(f"/erpsync/entries/{row.id}/review").text
    assert DASH in page
    assert "posts to the ledger" in page


def test_erpsync_remap_form_requires_factor_value(client, db_session, config, tmp_path):
    """R5: the flagged-row remap form marks factor_value client-side ``required`` so an
    empty submit gets a browser field prompt, not the bare "Failed: 422" the missing
    (no-default) field would otherwise trigger. Pins the attribute on the input."""
    ids = _stage_month(db_session, config, tmp_path)
    flagged = db_session.execute(
        select(ErpsyncEntry).where(
            ErpsyncEntry.client_id == ids["client"], ErpsyncEntry.status == "flagged"
        ).limit(1)
    ).scalar_one()
    page = client.get(f"/erpsync/entries/{flagged.id}/review").text
    match = re.search(r'<input[^>]*name="factor_value"[^>]*>', page)
    assert match, "factor_value input not rendered on the remap form"
    assert "required" in match.group(0), "factor_value input is not client-side required"
