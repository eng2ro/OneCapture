"""Vehicle registry (Appendix H-C) + claimant profile fields (H-B).

A mileage line picks ANY registered vehicle (claim-on-behalf / paid-in-advance),
defaulting to none; the vehicle_type is the CarbonNext distance-based input.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from eclaim.db.models import Claimant, ClaimLine, Vehicle
from eclaim.services import vehicles


def _vehicle(db_session, ids, label="WXY 1234", vtype="car_petrol", **kw) -> Vehicle:
    v = Vehicle(firm_id=ids["firm"], client_id=ids["client"], label=label,
                vehicle_type=vtype, **kw)
    db_session.add(v)
    db_session.flush()
    return v


# --------------------------------------------------------------------------- #
# Service resolution
# --------------------------------------------------------------------------- #
def test_resolve_is_client_scoped_and_tolerant(client, db_session):
    ids = db_session.info["principal"]
    v = _vehicle(db_session, ids)
    assert vehicles.resolve(db_session, ids["client"], str(v.id)).id == v.id
    assert vehicles.resolve(db_session, ids["client"], "") is None
    assert vehicles.resolve(db_session, ids["client"], "not-a-uuid") is None
    assert vehicles.resolve(db_session, uuid.uuid4(), str(v.id)) is None   # other client
    v.active = False
    db_session.flush()
    assert vehicles.resolve(db_session, ids["client"], str(v.id)) is None  # inactive


def test_usual_vehicle_for_claimant(client, db_session):
    ids = db_session.info["principal"]
    cm = Claimant(firm_id=ids["firm"], client_id=ids["client"], name="Aina", phone="+601")
    db_session.add(cm)
    db_session.flush()
    v = _vehicle(db_session, ids, label="Aina's Myvi", usual_claimant_id=cm.id)
    assert vehicles.usual_for(db_session, cm.id).id == v.id
    assert vehicles.usual_for(db_session, None) is None


# --------------------------------------------------------------------------- #
# Mileage line carries the vehicle
# --------------------------------------------------------------------------- #
def test_mileage_capture_records_the_vehicle(client, db_session, fake_ocr):
    """The legacy one-trip mileage endpoint stores the picked vehicle on the line
    and its type in the mileage evidence — the CarbonNext distance-based input."""
    import eclaim.api.deps as deps
    from eclaim.api.app import create_app  # noqa: F401 (app already built by fixture)

    ids = db_session.info["principal"]
    v = _vehicle(db_session, ids, label="WXY 5678", vtype="car_diesel")
    db_session.commit()

    r = client.post("/capture/mileage", data={
        "origin": "KL Sentral", "destination": "Putrajaya",
        "trip_date": "2026-07-01", "attested": "yes",
        "vehicle_id": str(v.id),
    }, follow_redirects=False)
    assert r.status_code == 303, r.text

    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.vehicle_id == v.id)
    ).scalars().one()
    assert line.expense_type == "mileage"
    assert line.mileage["vehicle_type"] == "car_diesel"
    assert line.mileage["vehicle_label"] == "WXY 5678"


def test_mileage_without_vehicle_still_files(client, db_session):
    r = client.post("/capture/mileage", data={
        "origin": "KL Sentral", "destination": "Shah Alam",
        "trip_date": "2026-07-02", "attested": "yes",
        "vehicle_id": "",
    }, follow_redirects=False)
    assert r.status_code == 303
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.expense_type == "mileage")
        .order_by(ClaimLine.created_at.desc())
    ).scalars().first()
    assert line.vehicle_id is None
    assert line.mileage["vehicle_type"] is None


# --------------------------------------------------------------------------- #
# Admin pages
# --------------------------------------------------------------------------- #
def test_admin_vehicle_crud_and_toggle(client, db_session):
    ids = db_session.info["principal"]
    r = client.post("/admin/vehicles", data={
        "client_id": str(ids["client"]), "label": "VAN 99",
        "vehicle_type": "van", "engine_size": "2.5L",
    }, follow_redirects=False)
    assert r.status_code == 303
    v = db_session.execute(select(Vehicle).where(Vehicle.label == "VAN 99")).scalars().one()
    assert v.vehicle_type == "van" and v.active

    # junk type rejected
    bad = client.post("/admin/vehicles", data={
        "client_id": str(ids["client"]), "label": "X", "vehicle_type": "rocket",
    })
    assert "Unknown vehicle type" in bad.text

    assert client.post("/admin/vehicles/toggle", data={"vehicle_id": str(v.id)},
                       follow_redirects=False).status_code == 303
    db_session.expire_all()
    assert db_session.get(Vehicle, v.id).active is False


def test_handoff_carries_cat6_travel_context(client, db_session):
    """Migration 0036: the CarbonNext Cat-6 record needs the employee (ref/name/
    position), travel purpose and vehicle type — all captured before, none
    forwarded. Release now stamps them on the handoff."""
    import uuid as _uuid

    from eclaim.db.models import CarbonHandoff, Claim

    ids = db_session.info["principal"]
    cm = Claimant(firm_id=ids["firm"], client_id=ids["client"], name="Aina",
                  phone="+60166", employee_ref="E-12", position="Sales Executive",
                  department="Sales")
    db_session.add(cm)
    db_session.flush()
    v = _vehicle(db_session, ids, label="Aina Bezza", vtype="car_petrol",
                 usual_claimant_id=cm.id)
    db_session.commit()

    r = client.post("/capture/mileage", data={
        "origin": "KL Sentral", "destination": "Melaka",
        "trip_date": "2026-07-03", "attested": "yes", "vehicle_id": str(v.id),
    }, follow_redirects=False)
    assert r.status_code == 303
    cid = r.headers["location"].split("/claims/")[1].split("/")[0]

    claim = db_session.get(Claim, _uuid.UUID(cid))
    claim.submitted_by_claimant_id = cm.id       # the traveler
    claim.purpose = "Client visit — Melaka"
    db_session.commit()

    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    assert client.post(f"/api/claims/{cid}/release").status_code == 200
    handoff = db_session.query(CarbonHandoff).filter_by(
        claim_id=claim.id, direction="forward"
    ).one()
    assert handoff.employee_ref == "E-12"
    assert handoff.employee_name == "Aina"
    assert handoff.position == "Sales Executive"
    assert handoff.travel_purpose == "Client visit — Melaka"
    assert handoff.vehicle_type == "car_petrol"
    assert handoff.unit == "km" and handoff.quantity is not None


def test_claimant_position_and_department_persist(client, db_session):
    """H-B: the Cat-6 employee fields (position/department) save via the admin page."""
    ids = db_session.info["principal"]
    r = client.post("/admin/claimants", data={
        "client_id": str(ids["client"]), "name": "Hafiz", "phone": "+60555",
        "employee_ref": "E-9", "position": "Sales Executive", "department": "Sales",
        "status": "active",
    }, follow_redirects=False)
    assert r.status_code == 303
    cm = db_session.execute(
        select(Claimant).where(Claimant.client_id == ids["client"], Claimant.phone == "+60555")
    ).scalar_one()
    assert cm.position == "Sales Executive" and cm.department == "Sales"
