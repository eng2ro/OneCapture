"""Admin web screens: category + claimant master (firm-scope only).

Partner flows go through the conftest ``client`` fixture (a firm partner);
role-block tests build a client with a viewer/approver principal; RLS tests
owner-seed another firm. Mutations persist through the same RLS-scoped session.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from eclaim.db.models import Category, Claimant, Client, Firm


def _client_as(db_session, fake_ocr, tmp_path, role: str):
    """A TestClient whose principal has the given role (for the role-gate tests)."""
    from fastapi.testclient import TestClient

    from eclaim.api import deps
    from eclaim.api.app import create_app
    from eclaim.auth.principal import Principal

    ids = db_session.info["principal"]

    def _override_db():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    def _principal() -> Principal:
        return Principal(
            user_id=ids["user"], firm_id=ids["firm"], base_role=role,
            allowed_client_ids=frozenset({ids["client"]}), email=f"{role}@seed.test",
        )

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    app.dependency_overrides[deps.get_session_principal] = _principal
    app.dependency_overrides[deps.get_principal] = _principal
    app.dependency_overrides[deps.get_ocr] = lambda: fake_ocr
    app.dependency_overrides[deps.get_image_dir] = lambda: tmp_path
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #
def test_partner_lists_and_creates_category(client, db_session):
    ids = db_session.info["principal"]
    page = client.get("/admin/categories")
    assert page.status_code == 200
    assert "Diesel" in page.text   # a seeded category is listed

    resp = client.post("/admin/categories", data={
        "client_id": str(ids["client"]), "name": "Stationery", "expense_type": "other",
        "factor_key": "", "gl_export_code": "GL-9000", "default_limit": "500", "status": "active",
    }, follow_redirects=False)
    assert resp.status_code == 303

    cat = db_session.execute(
        select(Category).filter_by(client_id=ids["client"], name="Stationery")
    ).scalar_one()
    assert cat.expense_type == "other" and cat.factor_key is None
    assert cat.gl_export_code == "GL-9000" and cat.default_limit == Decimal("500.00")


def test_partner_edits_category(client, db_session):
    ids = db_session.info["principal"]
    diesel = db_session.execute(
        select(Category).filter_by(client_id=ids["client"], expense_type="fuel_diesel")
    ).scalar_one()

    resp = client.post("/admin/categories", data={
        "category_id": str(diesel.id), "client_id": str(ids["client"]), "name": diesel.name,
        "expense_type": "fuel_diesel", "factor_key": "fuel_diesel",
        "gl_export_code": "GL-EDIT", "default_limit": "", "status": "active",
    }, follow_redirects=False)
    assert resp.status_code == 303

    db_session.refresh(diesel)
    assert diesel.gl_export_code == "GL-EDIT"


def test_duplicate_expense_type_now_allowed(client, db_session):
    """Since 0007, a client may have several categories sharing one carbon
    expense_type — a different name is all that's required."""
    ids = db_session.info["principal"]
    resp = client.post("/admin/categories", data={
        "client_id": str(ids["client"]), "name": "Dup Diesel", "expense_type": "fuel_diesel",
        "factor_key": "fuel_diesel", "gl_export_code": "", "default_limit": "", "status": "active",
    }, follow_redirects=False)
    assert resp.status_code == 303            # committed, not re-rendered with an error
    count = db_session.execute(
        select(func.count()).select_from(Category)
        .where(Category.client_id == ids["client"], Category.expense_type == "fuel_diesel")
    ).scalar_one()
    assert count == 2                         # the seeded one + the new one


def test_duplicate_category_name_still_errors(client, db_session):
    """Name stays the human-unique key per client (uq_category_client_name)."""
    ids = db_session.info["principal"]
    existing = db_session.execute(
        select(Category).where(Category.client_id == ids["client"])
    ).scalars().first()
    resp = client.post("/admin/categories", data={
        "client_id": str(ids["client"]), "name": existing.name, "expense_type": "other",
        "factor_key": "", "gl_export_code": "", "default_limit": "", "status": "active",
    }, follow_redirects=False)
    assert resp.status_code == 200            # re-rendered, not a redirect
    assert "already exists" in resp.text


# --------------------------------------------------------------------------- #
# Claimants
# --------------------------------------------------------------------------- #
def test_partner_lists_and_creates_claimant(client, db_session):
    ids = db_session.info["principal"]
    assert client.get("/admin/claimants").status_code == 200

    resp = client.post("/admin/claimants", data={
        "client_id": str(ids["client"]), "name": "Alice", "phone": "+60123",
        "email": "alice@x.test", "employee_ref": "E-7", "cost_centre": "CC-42", "status": "active",
    }, follow_redirects=False)
    assert resp.status_code == 303

    cm = db_session.execute(
        select(Claimant).filter_by(client_id=ids["client"], phone="+60123")
    ).scalar_one()
    assert cm.name == "Alice" and cm.employee_ref == "E-7" and cm.cost_centre == "CC-42"


def test_duplicate_claimant_phone_errors(client, db_session):
    ids = db_session.info["principal"]
    base = {"client_id": str(ids["client"]), "status": "active"}
    assert client.post("/admin/claimants", data={**base, "name": "Alice", "phone": "+60999"},
                       follow_redirects=False).status_code == 303

    resp = client.post("/admin/claimants", data={**base, "name": "Bob", "phone": "+60999"},
                       follow_redirects=False)
    assert resp.status_code == 200
    assert "already exists" in resp.text
    count = db_session.execute(
        select(func.count()).select_from(Claimant)
        .where(Claimant.client_id == ids["client"], Claimant.phone == "+60999")
    ).scalar_one()
    assert count == 1


# --------------------------------------------------------------------------- #
# Role gate
# --------------------------------------------------------------------------- #
def test_viewer_and_approver_blocked_from_admin(db_session, fake_ocr, tmp_path):
    for role in ("viewer", "approver"):
        c = _client_as(db_session, fake_ocr, tmp_path, role)
        assert c.get("/admin/categories").status_code == 403
        assert c.get("/admin/claimants").status_code == 403


# --------------------------------------------------------------------------- #
# RLS scoping
# --------------------------------------------------------------------------- #
def test_admin_lists_exclude_other_firms(client, db_session, db_engine):
    owner = Session(bind=db_engine, future=True, expire_on_commit=False)
    firm_b = None
    try:
        firm_b = Firm(name="Admin RLS Firm B")
        owner.add(firm_b)
        owner.flush()
        client_b = Client(firm_id=firm_b.id, name="B Co", currency="MYR")
        owner.add(client_b)
        owner.flush()
        owner.add(Category(
            firm_id=firm_b.id, client_id=client_b.id, name="ZZZ-Other-Firm-Cat",
            expense_type="other", status="active"))
        owner.add(Claimant(
            firm_id=firm_b.id, client_id=client_b.id, name="ZZZ-Other-Firm-Claimant",
            phone="+60000", status="active"))
        owner.flush()
        owner.commit()

        assert "ZZZ-Other-Firm-Cat" not in client.get("/admin/categories").text
        assert "ZZZ-Other-Firm-Claimant" not in client.get("/admin/claimants").text
    finally:
        if firm_b is not None:
            owner.execute(text("DELETE FROM category WHERE firm_id = :f"), {"f": firm_b.id})
            owner.execute(text("DELETE FROM claimant WHERE firm_id = :f"), {"f": firm_b.id})
            owner.execute(text("DELETE FROM client WHERE firm_id = :f"), {"f": firm_b.id})
            owner.execute(text("DELETE FROM firm WHERE id = :f"), {"f": firm_b.id})
            owner.commit()
        owner.close()
