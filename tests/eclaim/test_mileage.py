"""Phase C — mileage claims + map (Google Directions mocked)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from eclaim.db.models import Category, Claim, ClaimLine
from eclaim.maps import GoogleDirectionsProvider, MapError, RouteResult, _parse_routes


# --- routes parsing (no network) -------------------------------------------
def test_parse_routes_reads_distance_and_polyline():
    data = {
        "routes": [{
            "distanceMeters": 38200,
            "polyline": {"encodedPolyline": "abc123"},
            "legs": [{"distanceMeters": 20000}, {"distanceMeters": 18200}],
        }],
    }
    r = _parse_routes(data, "A", "B", ["mid"])
    assert r.distance_km == Decimal("38.200")
    assert r.polyline == "abc123"
    assert r.stops == 3 and len(r.legs) == 2


def test_parse_routes_raises_when_no_route():
    with pytest.raises(MapError):
        _parse_routes({"routes": []}, "A", "B", [])


def test_google_provider_requires_key():
    with pytest.raises(MapError):
        GoogleDirectionsProvider("")


# --- web flow (Directions provider mocked) ---------------------------------
class _FakeDirections:
    def route(self, origin, destination, waypoints=None):
        return RouteResult(
            distance_km=Decimal("38.200"), polyline="enc_xyz",
            legs=[{"from": origin, "to": destination, "km": "38.200"}],
        )

    def routes(self, origin, destination, waypoints=None):
        return [self.route(origin, destination, waypoints)]


class _FakeAltDirections:
    """Two routes: recommended 38.2 km (index 0), a longer 45.0 km alternative."""
    def route(self, origin, destination, waypoints=None):
        return self.routes(origin, destination, waypoints)[0]

    def routes(self, origin, destination, waypoints=None):
        return [
            RouteResult(distance_km=Decimal("38.200"), polyline="rec",
                        legs=[{"from": origin, "to": destination, "km": "38.200"}]),
            RouteResult(distance_km=Decimal("45.000"), polyline="alt",
                        description="via Federal Hwy",
                        legs=[{"from": origin, "to": destination, "km": "45.000"}]),
        ]


def _mileage_category(db_session):
    ids = db_session.info["principal"]
    cat = Category(firm_id=ids["firm"], client_id=ids["client"], name="Mileage",
                   expense_type="mileage", carbon_relevant=True)
    db_session.add(cat)
    db_session.flush()
    return cat


def test_mileage_claim_priced_from_server_distance(client, db_session, monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    cat = _mileage_category(db_session)

    resp = client.post("/capture/mileage", data={
        "origin": "KL Sentral", "destination": "Cyberjaya", "trip_date": "2026-03-12",
    }, follow_redirects=False)
    assert resp.status_code == 303

    claim = db_session.execute(select(Claim)).scalars().one()
    line = db_session.execute(
        select(ClaimLine).filter_by(claim_id=claim.id)
    ).scalar_one()
    assert line.expense_type == "mileage"
    assert line.quantity == Decimal("38.200") and line.unit == "km"
    assert line.total_amount == Decimal("22.92")   # 38.200 km × RM 0.60/km
    assert line.category_id == cat.id and line.carbon_relevant is True
    assert line.image_path is None                  # no receipt — route is evidence
    assert line.mileage["origin"] == "KL Sentral"
    assert line.mileage["distance_km"] == "38.200"
    assert claim.claim_type == "travel"             # dated trip


def test_preview_returns_alternatives_recommended_first(client, monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeAltDirections())
    r = client.post("/capture/mileage/preview",
                    data={"origin": "A", "destination": "B", "waypoints": "[]"})
    body = r.json()
    assert body["ok"] is True
    assert body["recommended_km"] == "38.200"
    assert [x["distance_km"] for x in body["routes"]] == ["38.200", "45.000"]
    assert body["routes"][1]["description"] == "via Federal Hwy"


def test_chosen_longer_route_is_reimbursed_and_flagged(client, db_session, monkeypatch):
    """Pick the longer alternative (index 1): reimburse the chosen distance, keep the
    recommended one, and flag the overage for the approver."""
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeAltDirections())
    _mileage_category(db_session)

    resp = client.post("/capture/mileage", data={
        "origin": "A", "destination": "B", "route_index": "1",
    }, follow_redirects=False)
    assert resp.status_code == 303

    line = db_session.execute(select(ClaimLine)).scalar_one()
    assert line.quantity == Decimal("45.000")            # chosen route reimbursed
    assert line.total_amount == Decimal("27.00")         # 45.000 km × RM 0.60/km
    assert line.mileage["recommended_km"] == "38.200"
    assert line.mileage["over_recommended"] is True
    assert line.mileage["route_description"] == "via Federal Hwy"


def test_recommended_route_not_flagged(client, db_session, monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeAltDirections())
    _mileage_category(db_session)
    client.post("/capture/mileage", data={"origin": "A", "destination": "B"},
                follow_redirects=False)   # route_index defaults to 0 (recommended)
    line = db_session.execute(select(ClaimLine)).scalar_one()
    assert line.quantity == Decimal("38.200")
    assert line.mileage["over_recommended"] is False


def test_mileage_requires_from_and_to(client, db_session, monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    resp = client.post("/capture/mileage", data={"origin": "KL", "destination": ""},
                       follow_redirects=False)
    assert resp.status_code == 200
    assert "From and To are both required" in resp.text
    assert db_session.execute(select(Claim)).scalars().first() is None


def test_add_mileage_line_to_existing_receipt_claim(client, db_session, monkeypatch):
    """Mileage can be added to a claim that already has receipts (review screen)."""
    import json
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    _mileage_category(db_session)

    # 1) capture a receipt claim
    client.post("/capture",
                files=[("files", ("r.png", b"\x89PNG\r\n fake", "image/png"))],
                data={"items": json.dumps([
                    {"expense_type": "other", "total_amount": "20", "vendor": "Cafe"}])},
                follow_redirects=False)
    claim = db_session.execute(select(Claim)).scalars().one()

    # 2) add a mileage line to it from the review screen
    r = client.post(f"/claims/{claim.id}/mileage",
                    data={"origin": "A", "destination": "B"}, follow_redirects=False)
    assert r.status_code == 303
    lines = db_session.execute(
        select(ClaimLine).filter_by(claim_id=claim.id).order_by(ClaimLine.line_no)
    ).scalars().all()
    assert len(lines) == 2
    mil = next(l for l in lines if l.expense_type == "mileage")
    assert mil.quantity == Decimal("38.200")
    assert mil.total_amount == Decimal("22.92")


def test_cannot_add_mileage_to_released_claim(client, db_session, monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    _mileage_category(db_session)
    client.post("/capture/mileage", data={"origin": "A", "destination": "B"},
                follow_redirects=False)
    claim = db_session.execute(select(Claim)).scalars().one()
    assert client.post(f"/api/claims/{claim.id}/approve").status_code == 200
    assert client.post(f"/api/claims/{claim.id}/release").status_code == 200
    # released → adding a line is rejected (re-renders review with the error)
    r = client.post(f"/claims/{claim.id}/mileage",
                    data={"origin": "C", "destination": "D"}, follow_redirects=False)
    assert r.status_code == 200
    assert "cannot add a line" in r.text


def test_review_shows_route_map_for_mileage(client, db_session, monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    _mileage_category(db_session)
    client.post("/capture/mileage", data={
        "origin": "KL Sentral", "destination": "Cyberjaya",
    }, follow_redirects=False)
    claim = db_session.execute(select(Claim)).scalars().one()
    page = client.get(f"/claims/{claim.id}/review").text
    assert "LINE_MILEAGE" in page and "route-summary" in page
    assert "KL Sentral" in page
