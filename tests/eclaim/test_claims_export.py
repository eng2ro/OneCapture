"""CSV export of claims for the accounting / ERP reimbursement system
(GET /api/claims/export).

The export is now PER LINE (a claim is a header with N lines): one row per
claim_line, carrying the claimant/category joins, the line's payment method and
``line_status``, and ``carbon_class`` — but NO tCO2e/scope (the carbon split is on
the Carbon Next side). Claims + lines are seeded directly via ``db_session`` (the
RLS-enforced app-role session the TestClient uses) for control over the joins,
status, and created_at. Covers: header + a row per line, GL from the category
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

from eclaim.db.models import Category, Claim, Claimant, ClaimLine, Client, Firm

HEADER = [
    "claim_id", "line_no", "doc_date", "claim_status", "line_status",
    "claimant_name", "employee_ref", "cost_centre", "vendor", "doc_no",
    "category_name", "gl_code", "payment_method", "reimbursable",
    "currency", "total_amount", "tax_amount", "tax_code", "net_amount",
    "fx_rate", "base_amount", "posting_date", "department", "project_code",
    "supplier_tax_id", "carbon_relevant", "release_batch_id",
]


def _rows(resp):
    return list(csv.reader(io.StringIO(resp.text)))


def _col(row, name):
    return row[HEADER.index(name)]


def _claim(db_session, ids, *, lines=(("approved", {}),), status="released", **kw) -> Claim:
    """A claim header + one line per ``lines`` entry (line_status, line-field
    overrides). Defaults to a single approved line."""
    claim = Claim(firm_id=ids["firm"], client_id=ids["client"], status=status, **kw)
    db_session.add(claim)
    db_session.flush()
    for i, (line_status, linekw) in enumerate(lines, start=1):
        base = dict(
            firm_id=ids["firm"], client_id=ids["client"], claim_id=claim.id, line_no=i,
            image_path="/x.png", image_sha256="h", currency="MYR",
            payment_method="out_of_pocket", reimbursable=True, carbon_relevant=True,
            line_status=line_status,
        )
        base.update(linekw)
        db_session.add(ClaimLine(**base))
    db_session.flush()
    return claim


# 1 -------------------------------------------------------------------------
def test_export_header_and_row_per_line_with_joins(client, db_session):
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
        db_session, ids, submitted_by_claimant_id=claimant.id,
        lines=[("approved", dict(
            category_id=cat.id, vendor="Shell", doc_no="INV-1", doc_date="2026-03-01",
            total_amount=Decimal("100.00"), carbon_relevant=True,
        ))],
    )
    c2 = _claim(  # uncategorized, no claimant
        db_session, ids,
        lines=[("approved", dict(vendor="Acme", doc_no="INV-2", total_amount=Decimal("50.00")))],
    )

    resp = client.get("/api/claims/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")

    rows = _rows(resp)
    assert rows[0] == HEADER
    data = {r[0]: r for r in rows[1:]}
    assert len(data) == 2  # one row per line (each claim has one line)

    r1 = data[str(c1.id)]
    assert _col(r1, "claimant_name") == "Alice"
    assert _col(r1, "employee_ref") == "E-7"
    assert _col(r1, "cost_centre") == "CC-42"          # from the claimant
    assert _col(r1, "category_name") == cat.name
    assert _col(r1, "gl_code") == "GL-5000"     # resolved from the category default
    assert _col(r1, "total_amount") == "100.00"
    assert _col(r1, "payment_method") == "out_of_pocket"
    assert _col(r1, "carbon_relevant") == "True"
    assert _col(r1, "line_status") == "approved"

    r2 = data[str(c2.id)]
    assert _col(r2, "category_name") == ""             # uncategorized → blank
    assert _col(r2, "gl_code") == ""
    assert _col(r2, "cost_centre") == ""               # no claimant → blank
    assert _col(r2, "claimant_name") == ""


# 2 -------------------------------------------------------------------------
def test_export_status_filter(client, db_session):
    ids = db_session.info["principal"]
    rel = _claim(db_session, ids, status="released",
                 lines=[("approved", dict(total_amount=Decimal("10")))])
    rev = _claim(db_session, ids, status="in_review",
                 lines=[("pending", dict(total_amount=Decimal("20")))])

    released = {r[0] for r in _rows(client.get("/api/claims/export"))[1:]}
    assert str(rel.id) in released and str(rev.id) not in released

    in_review = {r[0] for r in _rows(client.get("/api/claims/export?status=in_review"))[1:]}
    assert str(rev.id) in in_review and str(rel.id) not in in_review


# 3 -------------------------------------------------------------------------
def test_export_date_filter(client, db_session):
    ids = db_session.info["principal"]
    c = _claim(
        db_session, ids, created_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
        lines=[("approved", dict(total_amount=Decimal("10")))],
    )

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
    """A claim/line in another firm (owner-seeded + committed, since the app role's
    RLS would block inserting it) never appears in this principal's export."""
    ids = db_session.info["principal"]
    mine = _claim(db_session, ids, lines=[("approved", dict(total_amount=Decimal("10")))])

    owner = Session(bind=db_engine, future=True, expire_on_commit=False)
    firm_b = None
    try:
        firm_b = Firm(name="Export RLS Firm B")
        owner.add(firm_b)
        owner.flush()
        client_b = Client(firm_id=firm_b.id, name="B Co", currency="MYR")
        owner.add(client_b)
        owner.flush()
        other = Claim(firm_id=firm_b.id, client_id=client_b.id, status="released", currency="MYR")
        owner.add(other)
        owner.flush()
        owner.add(ClaimLine(
            firm_id=firm_b.id, client_id=client_b.id, claim_id=other.id, line_no=1,
            image_path="/o.png", image_sha256="o", total_amount=Decimal("99"),
            line_status="approved", carbon_relevant=True,
        ))
        owner.flush()
        owner.commit()
        other_id = str(other.id)

        ids_in_export = {r[0] for r in _rows(client.get("/api/claims/export"))[1:]}
        assert str(mine.id) in ids_in_export   # own firm visible
        assert other_id not in ids_in_export   # other firm RLS-excluded
    finally:
        if firm_b is not None:
            owner.execute(text("DELETE FROM claim_line WHERE firm_id = :f"), {"f": firm_b.id})
            owner.execute(text("DELETE FROM claim WHERE firm_id = :f"), {"f": firm_b.id})
            owner.execute(text("DELETE FROM client WHERE firm_id = :f"), {"f": firm_b.id})
            owner.execute(text("DELETE FROM firm WHERE id = :f"), {"f": firm_b.id})
            owner.commit()
        owner.close()
