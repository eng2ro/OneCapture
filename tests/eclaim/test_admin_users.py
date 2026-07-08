"""Firm user administration (/admin/users): the login-user registry an admin
maintains in OneCapture (Appendix I-B). Partner/manager only; lockout guards
mean a firm can never remove its own last administrator by accident.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import select

from eclaim.db.models import AppUser, AuditEvent, UserClientGrant


def _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, base_role: str, user_id=None):
    """TestClient with the principal forced to ``base_role`` (test_web_auth pattern);
    ``user_id`` lets a test act as a specific DB user (default: the seeded partner)."""
    from eclaim.api import deps
    from eclaim.api.app import create_app
    from eclaim.auth.principal import Principal

    def _override_db():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    def _principal(request: Request) -> Principal:
        ids = db_session.info["principal"]
        principal = Principal(
            user_id=user_id or ids["user"], firm_id=ids["firm"], base_role=base_role,
            allowed_client_ids=frozenset({ids["client"]}), email=f"{base_role}@seed.test",
        )
        request.state.principal = principal
        request.state.db = db_session
        return principal

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    app.dependency_overrides[deps.get_principal] = _principal
    app.dependency_overrides[deps.get_session_principal] = _principal
    app.dependency_overrides[deps.get_ocr] = lambda: fake_ocr
    app.dependency_overrides[deps.get_segmenter] = lambda: fake_segmenter
    app.dependency_overrides[deps.get_image_dir] = lambda: tmp_path
    return app


def _user_by_email(db_session, email: str) -> AppUser | None:
    return db_session.execute(
        select(AppUser).where(AppUser.email == email)
    ).scalar_one_or_none()


def _audit(db_session, user_id) -> list[AuditEvent]:
    return list(db_session.execute(
        select(AuditEvent)
        .where(AuditEvent.entity_type == "app_user", AuditEvent.entity_id == user_id)
        .order_by(AuditEvent.created_at)
    ).scalars())


def _grants(db_session, user_id) -> list[uuid.UUID]:
    return [g.client_id for g in db_session.execute(
        select(UserClientGrant).where(UserClientGrant.user_id == user_id)
    ).scalars()]


def test_partner_creates_a_client_scoped_user_with_grants(client, db_session):
    ids = db_session.info["principal"]
    r = client.post("/admin/users", data={
        "email": "Anis@Firm.test", "display_name": "Anis",
        "base_role": "approver", "authority_limit": "5000",
        "status": "active", "grant_client_ids": [str(ids["client"])],
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/users"

    u = _user_by_email(db_session, "anis@firm.test")     # email is normalised
    assert u is not None and u.base_role == "approver"
    assert u.authority_limit == Decimal("5000")
    assert _grants(db_session, u.id) == [ids["client"]]
    events = _audit(db_session, u.id)
    assert [e.event_type for e in events] == ["user_created"]

    page = client.get("/admin/users").text
    assert "anis@firm.test" in page and "Anis" in page


def test_edit_promotes_to_manager_and_clears_grants(client, db_session):
    ids = db_session.info["principal"]
    client.post("/admin/users", data={
        "email": "b@firm.test", "display_name": "B", "base_role": "viewer",
        "status": "active", "grant_client_ids": [str(ids["client"])],
    }, follow_redirects=False)
    u = _user_by_email(db_session, "b@firm.test")

    r = client.post("/admin/users", data={
        "user_id": str(u.id), "email": "b@firm.test", "display_name": "B",
        "base_role": "manager", "authority_limit": "", "status": "active",
    }, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert _user_by_email(db_session, "b@firm.test").base_role == "manager"
    # Firm-scoped users hold no client grants — no stale hidden access to
    # inherit if they are later demoted back to a client-scoped role.
    assert _grants(db_session, u.id) == []
    update = _audit(db_session, u.id)[-1]
    assert update.event_type == "user_updated"
    assert update.detail["base_role"] == {"from": "viewer", "to": "manager"}


def test_duplicate_email_is_a_friendly_error(client, db_session):
    ids = db_session.info["principal"]
    form = {"email": "dup@firm.test", "display_name": "One", "base_role": "approver",
            "status": "active", "grant_client_ids": [str(ids["client"])]}
    assert client.post("/admin/users", data=form, follow_redirects=False).status_code == 303
    again = client.post("/admin/users", data=form | {"display_name": "Two"},
                        follow_redirects=False)
    assert again.status_code == 200 and "already exists" in again.text


def test_you_cannot_deactivate_or_demote_yourself(client, db_session):
    ids = db_session.info["principal"]
    me = db_session.get(AppUser, ids["user"])
    base = {"user_id": str(ids["user"]), "email": me.email,
            "display_name": me.display_name}

    r = client.post("/admin/users", data=base | {
        "base_role": "partner", "status": "inactive"}, follow_redirects=False)
    assert r.status_code == 200 and "cannot deactivate your own" in r.text

    r = client.post("/admin/users", data=base | {
        "base_role": "viewer", "status": "active",
        "grant_client_ids": [str(ids["client"])]}, follow_redirects=False)
    assert r.status_code == 200 and "demote your own" in r.text

    db_session.expire_all()
    me = db_session.get(AppUser, ids["user"])
    assert me.status == "active" and me.base_role == "partner"   # untouched


def test_client_scoped_role_requires_at_least_one_company(client, db_session):
    r = client.post("/admin/users", data={
        "email": "lone@firm.test", "display_name": "Lone",
        "base_role": "viewer", "status": "active",
    }, follow_redirects=False)
    assert r.status_code == 200 and "at least one company" in r.text
    assert _user_by_email(db_session, "lone@firm.test") is None


def test_cannot_grant_a_company_outside_the_firm(client, db_session):
    r = client.post("/admin/users", data={
        "email": "x@firm.test", "display_name": "X", "base_role": "approver",
        "status": "active", "grant_client_ids": [str(uuid.uuid4())],
    }, follow_redirects=False)
    assert r.status_code == 200 and "outside your firm" in r.text
    assert _user_by_email(db_session, "x@firm.test") is None


def test_deactivating_another_user_works_and_is_audited(client, db_session):
    ids = db_session.info["principal"]
    client.post("/admin/users", data={
        "email": "leaver@firm.test", "display_name": "Leaver", "base_role": "approver",
        "status": "active", "grant_client_ids": [str(ids["client"])],
    }, follow_redirects=False)
    u = _user_by_email(db_session, "leaver@firm.test")

    r = client.post("/admin/users", data={
        "user_id": str(u.id), "email": "leaver@firm.test", "display_name": "Leaver",
        "base_role": "approver", "status": "inactive",
        "grant_client_ids": [str(ids["client"])],
    }, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert _user_by_email(db_session, "leaver@firm.test").status == "inactive"
    assert _audit(db_session, u.id)[-1].detail["status"] == {
        "from": "active", "to": "inactive"}


def _make_manager(client, db_session, email="mgr@firm.test", limit="5000"):
    client.post("/admin/users", data={
        "email": email, "display_name": "Mgr", "base_role": "manager",
        "authority_limit": limit, "status": "active",
    }, follow_redirects=False)
    return _user_by_email(db_session, email)


def test_manager_cannot_self_promote_or_lift_own_cap(client, db_session, fake_ocr,
                                                     fake_segmenter, tmp_path):
    """Review finding: the lockout guard must be symmetric — no self-service
    ESCALATION (role up, cap up/removed), not just no self-demotion."""
    m = _make_manager(client, db_session)
    app = _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, "manager", user_id=m.id)
    with TestClient(app) as c:
        base = {"user_id": str(m.id), "email": m.email, "display_name": m.display_name,
                "status": "active"}
        r = c.post("/admin/users", data=base | {
            "base_role": "partner", "authority_limit": "5000"}, follow_redirects=False)
        assert r.status_code == 200 and "senior to your own" in r.text
        r = c.post("/admin/users", data=base | {
            "base_role": "manager", "authority_limit": ""}, follow_redirects=False)
        assert r.status_code == 200 and "your own authority limit" in r.text
    db_session.expire_all()
    m = _user_by_email(db_session, "mgr@firm.test")
    assert m.base_role == "manager" and m.authority_limit == Decimal("5000")


def test_manager_cannot_mint_or_edit_partners(client, db_session, fake_ocr,
                                              fake_segmenter, tmp_path):
    ids = db_session.info["principal"]
    m = _make_manager(client, db_session, email="mgr2@firm.test")
    partner = db_session.get(AppUser, ids["user"])          # the seeded partner
    app = _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, "manager", user_id=m.id)
    with TestClient(app) as c:
        r = c.post("/admin/users", data={
            "email": "newpartner@firm.test", "display_name": "NP",
            "base_role": "partner", "status": "active"}, follow_redirects=False)
        assert r.status_code == 200 and "senior to your own" in r.text
        # Editing a partner at all — here trying to demote+deactivate them — is
        # off-limits for a manager (posting base_role=partner would already trip
        # the assign guard above, so use a non-senior role to reach this check).
        r = c.post("/admin/users", data={
            "user_id": str(partner.id), "email": partner.email,
            "display_name": partner.display_name, "base_role": "manager",
            "status": "inactive"}, follow_redirects=False)
        assert r.status_code == 200 and "senior to your own role" in r.text
    db_session.expire_all()
    assert _user_by_email(db_session, "newpartner@firm.test") is None
    assert db_session.get(AppUser, ids["user"]).status == "active"


def test_authority_limit_junk_gets_a_friendly_error_not_a_500(client, db_session):
    ids = db_session.info["principal"]
    base = {"email": "cap@firm.test", "display_name": "Cap", "base_role": "approver",
            "status": "active", "grant_client_ids": [str(ids["client"])]}
    for bad, msg in (("NaN", "must be a number"), ("Infinity", "must be a number"),
                     ("9999999999999", "too large")):
        r = client.post("/admin/users", data=base | {"authority_limit": bad},
                        follow_redirects=False)
        assert r.status_code == 200 and msg in r.text
    assert _user_by_email(db_session, "cap@firm.test") is None


def test_login_accepts_any_email_case(browser):
    r = browser.post("/login", data={"email": "  Partner@Seed.Test "},
                     follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/claims"


def test_error_rerender_keeps_the_edit_context(client, db_session):
    """A refused UPDATE must come back as the same edit form (hidden user_id
    intact) — never as a blank Add form that would create a duplicate."""
    ids = db_session.info["principal"]
    me = db_session.get(AppUser, ids["user"])
    r = client.post("/admin/users", data={
        "user_id": str(me.id), "email": me.email, "display_name": me.display_name,
        "base_role": "partner", "status": "inactive"}, follow_redirects=False)
    assert r.status_code == 200 and "cannot deactivate your own" in r.text
    assert f'name="user_id" value="{me.id}"' in r.text
    assert "Edit user" in r.text


def test_users_page_is_firm_scope_only(db_session, fake_ocr, fake_segmenter, tmp_path):
    for role in ("viewer", "approver"):
        app = _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, role)
        with TestClient(app) as c:
            assert c.get("/admin/users").status_code == 403
            denied = c.post("/admin/users", data={
                "email": "evil@firm.test", "display_name": "Evil",
                "base_role": "partner", "status": "active",
            }, follow_redirects=False)
            assert denied.status_code == 403
    assert _user_by_email(db_session, "evil@firm.test") is None
