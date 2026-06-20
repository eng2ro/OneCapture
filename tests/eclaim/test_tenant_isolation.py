"""API-level tenant-isolation tests for the firm/client directory.

The point under test is point 2 of the multitenant spine: RLS on ``client`` /
``app_user`` / ``user_client_grant`` is *firm-gated only* (it must be, so
principal bootstrap can read the firm-wide client roster before the allowed
client set is known). Narrowing those listings to a client-scoped user's granted
clients is therefore the **app layer's** responsibility, and these tests pin it
at the API surface: a client-scoped Approver must see only its granted clients,
never a sibling client in the same firm.

Like the rest of the e-Claim DB tests these build the schema from the Alembic
migration and SKIP when no Postgres test DB is reachable (``db_engine`` fixture).
The seeding runs on the owner connection (RLS bypassed) because that is exactly
where the app-layer narrowing — not RLS — is what keeps clients apart; an owner
connection proves the Python/SQL cut holds even with RLS out of the picture.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from eclaim.api import deps
from eclaim.api.app import create_app
from eclaim.auth import tokens
from eclaim.config import get_settings
from eclaim.db.models import AppUser, Client, Firm, UserClientGrant


@pytest.fixture
def iso_session(db_engine) -> Session:
    """A session in a rolled-back outer transaction, seeded with one firm that
    owns two clients, a firm-scoped partner, and a client-scoped approver granted
    to exactly one of the two clients."""
    connection = db_engine.connect()
    trans = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
        future=True,
    )

    firm = Firm(name="Iso Firm")
    session.add(firm)
    session.flush()

    granted = Client(firm_id=firm.id, name="Granted Client", currency="MYR")
    sibling = Client(firm_id=firm.id, name="Sibling Client", currency="MYR")
    session.add_all([granted, sibling])
    session.flush()

    approver = AppUser(
        firm_id=firm.id, email="approver@iso.test",
        display_name="Appro Ver", base_role="approver",
    )
    partner = AppUser(
        firm_id=firm.id, email="partner@iso.test",
        display_name="Part Ner", base_role="partner",
    )
    session.add_all([approver, partner])
    session.flush()

    session.add(UserClientGrant(firm_id=firm.id, user_id=approver.id, client_id=granted.id))
    session.flush()

    session.info["ids"] = {
        "firm": firm.id,
        "granted": granted.id,
        "sibling": sibling.id,
        "approver": approver.id,
        "partner": partner.id,
    }
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


def _client_for(iso_session):
    """TestClient whose get_db yields the seeded session (committing on a clean
    request, rolling back on error) — the real auth/principal path is left intact
    so requests resolve a genuine Principal from the bearer token."""
    from fastapi.testclient import TestClient

    def _override_db():
        try:
            yield iso_session
            iso_session.commit()
        except Exception:
            iso_session.rollback()
            raise

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    return TestClient(app)


def _token(iso_session, who: str) -> str:
    ids = iso_session.info["ids"]
    role = "approver" if who == "approver" else "partner"
    return tokens.mint(
        {"user_id": str(ids[who]), "firm_id": str(ids["firm"]), "base_role": role},
        secret=get_settings().jwt_secret,
        ttl_seconds=300,
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_client_scoped_approver_sees_only_granted_clients(iso_session):
    """The crux: an Approver granted one client must NOT see the sibling client
    in the same firm, even though RLS would let the firm-gated table return it."""
    ids = iso_session.info["ids"]
    with _client_for(iso_session) as c:
        resp = c.get("/api/clients", headers=_auth(_token(iso_session, "approver")))

    assert resp.status_code == 200
    returned = {row["id"] for row in resp.json()}
    assert returned == {str(ids["granted"])}
    assert str(ids["sibling"]) not in returned  # the isolation assertion


def test_firm_scoped_partner_sees_every_client_in_firm(iso_session):
    """Positive control: a firm-scoped Partner sees the whole firm roster, so the
    Approver's narrower view above is the narrowing — not an empty database."""
    ids = iso_session.info["ids"]
    with _client_for(iso_session) as c:
        resp = c.get("/api/clients", headers=_auth(_token(iso_session, "partner")))

    assert resp.status_code == 200
    returned = {row["id"] for row in resp.json()}
    assert returned == {str(ids["granted"]), str(ids["sibling"])}
