"""CSV export of claims for the accounting system (GET /api/claims/export).

Claims are seeded directly via ``db_session`` (the RLS-enforced app-role session
the TestClient uses) for control over the claimant/category joins, status, and
created_at. Covers: header + a row per released claim, GL from the category
(blank when uncategorized), cost_centre from the claimant, RLS excluding another
firm, and the status/date filters.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from eclaim.db.models import Category, Claim, Claimant, Client, Firm

HEADER = [
    "claim_id", "doc_date", "status", "claimant_name", "employee_ref", "cost_centre",
    "vendor", "doc_no", "category_name", "gl_export_code", "currency", "total_amount",
    "scope", "basis", "tco2e", "factor_key", "release_batch_id",
]


def _rows(resp):
    return list(csv.reader(io.StringIO(resp.text)))


def _col(row, name):
    return row[HEADER.index(name)]


def _claim(ids, **kw) -> Claim:
    base = dict(
        firm_id=ids["firm"], client_id=ids["client"],
        image_path="/x.png", image_sha256="h", status="released", currency="MYR",
    )
    base.update(kw)
    return Claim(**base)


# 1 -------------------------------------------------------------------------
def test_export_header_and_row_per_claim_with_joins(client, db_session):
    ids = db_session.info["principal"]
    claimant = Claimant(
        firm_id=ids["firm"], client_id=ids["client"], name="Alice",
        phone="+60123456", employee_ref="E-7", cost_centre="CC-42",
    )
    db_session.add(claimant)
    db_session.flush()

    cat = db_session.execute(
        select(Category).filter_by(client_id=ids["client"], expense_type="fuel_diesel")
    ).scalar_one()
    cat.gl_export_code = "GL-5000"
    db_session.flush()

    c1 = _claim(
        ids, image_sha256="a", submitted_by_claimant_id=claimant.id, category_id=cat.id,
        vendor="Shell", doc_no="INV-1", doc_date="2026-03-01", total_amount=Decimal("100.00"),
        scope=1, basis="activity", tco2e=Decimal("1.206000"), factor_key="fuel_diesel",
    )
    c2 = _claim(  # uncategorized, no claimant
        ids, image_sha256="b", vendor="Acme", doc_no="INV-2", total_amount=Decimal("50.00"),
        scope=3, basis="spend", tco2e=Decimal("0.017500"), factor_key="spend_eeio",
    )
    db_session.add_all([c1, c2])
    db_session.flush()

    resp = client.get("/api/claims/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")

    rows = _rows(resp)
    assert rows[0] == HEADER
    data = {r[0]: r for r in rows[1:]}
    assert len(data) == 2  # one row per released claim

    r1 = data[str(c1.id)]
    assert _col(r1, "claimant_name") == "Alice"
    assert _col(r1, "employee_ref") == "E-7"
    assert _col(r1, "cost_centre") == "CC-42"          # from the claimant
    assert _col(r1, "category_name") == cat.name
    assert _col(r1, "gl_export_code") == "GL-5000"     # from the category
    assert _col(r1, "total_amount") == "100.00"
    assert _col(r1, "tco2e") == "1.206000"

    r2 = data[str(c2.id)]
    assert _col(r2, "category_name") == ""             # uncategorized → blank
    assert _col(r2, "gl_export_code") == ""
    assert _col(r2, "cost_centre") == ""               # no claimant → blank
    assert _col(r2, "claimant_name") == ""


# 2 -------------------------------------------------------------------------
def test_export_status_filter(client, db_session):
    ids = db_session.info["principal"]
    rel = _claim(ids, image_sha256="r", status="released", total_amount=Decimal("10"))
    rev = _claim(ids, image_sha256="i", status="in_review", total_amount=Decimal("20"))
    db_session.add_all([rel, rev])
    db_session.flush()

    released = {r[0] for r in _rows(client.get("/api/claims/export"))[1:]}
    assert str(rel.id) in released and str(rev.id) not in released

    in_review = {r[0] for r in _rows(client.get("/api/claims/export?status=in_review"))[1:]}
    assert str(rev.id) in in_review and str(rel.id) not in in_review


# 3 -------------------------------------------------------------------------
def test_export_date_filter(client, db_session):
    ids = db_session.info["principal"]
    c = _claim(
        ids, image_sha256="d", total_amount=Decimal("10"),
        created_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
    )
    db_session.add(c)
    db_session.flush()

    after = {r[0] for r in _rows(client.get("/api/claims/export?date_from=2026-06-01"))[1:]}
    assert str(c.id) not in after  # captured in March, excluded from a June start

    within = {
        r[0] for r in _rows(
            client.get("/api/claims/export?date_from=2026-01-01&date_to=2026-12-31")
        )[1:]
    }
    assert str(c.id) in within


# 4 -------------------------------------------------------------------------
def test_export_rls_excludes_other_firms(client, db_session, db_engine):
    """A claim in another firm (owner-seeded + committed, since the app role's RLS
    would block inserting it) never appears in this principal's export."""
    ids = db_session.info["principal"]
    mine = _claim(ids, image_sha256="m", total_amount=Decimal("10"))
    db_session.add(mine)
    db_session.flush()

    owner = Session(bind=db_engine, future=True, expire_on_commit=False)
    firm_b = None
    try:
        firm_b = Firm(name="Export RLS Firm B")
        owner.add(firm_b)
        owner.flush()
        client_b = Client(firm_id=firm_b.id, name="B Co", currency="MYR")
        owner.add(client_b)
        owner.flush()
        other = Claim(
            firm_id=firm_b.id, client_id=client_b.id, image_path="/o.png",
            image_sha256="o", status="released", currency="MYR", total_amount=Decimal("99"),
        )
        owner.add(other)
        owner.flush()
        owner.commit()
        other_id = str(other.id)

        ids_in_export = {r[0] for r in _rows(client.get("/api/claims/export"))[1:]}
        assert str(mine.id) in ids_in_export   # own firm visible
        assert other_id not in ids_in_export   # other firm RLS-excluded
    finally:
        if firm_b is not None:
            owner.execute(text("DELETE FROM claim WHERE firm_id = :f"), {"f": firm_b.id})
            owner.execute(text("DELETE FROM client WHERE firm_id = :f"), {"f": firm_b.id})
            owner.execute(text("DELETE FROM firm WHERE id = :f"), {"f": firm_b.id})
            owner.commit()
        owner.close()
