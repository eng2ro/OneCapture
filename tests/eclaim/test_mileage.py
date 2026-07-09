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
        "attested": "yes",
    }, follow_redirects=False)
    assert resp.status_code == 303

    claim = db_session.execute(select(Claim)).scalars().one()
    line = db_session.execute(
        select(ClaimLine).filter_by(claim_id=claim.id)
    ).scalar_one()
    assert claim.attested_by is not None            # mileage attestation recorded
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
    assert body["shortest_km"] == "38.200"
    assert [x["distance_km"] for x in body["routes"]] == ["38.200", "45.000"]
    assert body["routes"][1]["description"] == "via Federal Hwy"


def test_chosen_longer_route_is_reimbursed_and_flagged(client, db_session, monkeypatch):
    """Pick the longer alternative (index 1): reimburse the chosen distance, keep the
    recommended one, and flag the overage for the approver."""
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeAltDirections())
    _mileage_category(db_session)

    resp = client.post("/capture/mileage", data={
        "origin": "A", "destination": "B", "route_index": "1", "trip_date": "2026-03-12",
        "attested": "yes",
    }, follow_redirects=False)
    assert resp.status_code == 303

    line = db_session.execute(select(ClaimLine)).scalar_one()
    assert line.quantity == Decimal("45.000")            # chosen route reimbursed
    assert line.total_amount == Decimal("27.00")         # 45.000 km × RM 0.60/km
    assert line.mileage["shortest_km"] == "38.200"
    assert line.mileage["over_shortest"] is True
    assert line.mileage["route_description"] == "via Federal Hwy"


def test_recommended_route_not_flagged(client, db_session, monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeAltDirections())
    _mileage_category(db_session)
    client.post("/capture/mileage",
                data={"origin": "A", "destination": "B", "trip_date": "2026-03-12",
                      "attested": "yes"},
                follow_redirects=False)   # route_index defaults to 0 (recommended)
    line = db_session.execute(select(ClaimLine)).scalar_one()
    assert line.quantity == Decimal("38.200")
    assert line.mileage["over_shortest"] is False


def test_mileage_requires_from_and_to(client, db_session, monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    resp = client.post("/capture/mileage", data={"origin": "KL", "destination": ""},
                       follow_redirects=False)
    assert resp.status_code == 200
    assert "From and To are both required" in resp.text
    assert db_session.execute(select(Claim)).scalars().first() is None


def test_mileage_requires_trip_date(client, db_session, monkeypatch):
    """A mileage claim must carry a trip date (compulsory) — server-side, not only
    the HTML required attribute."""
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    _mileage_category(db_session)
    resp = client.post("/capture/mileage",
                       data={"origin": "A", "destination": "B"},  # no trip_date
                       follow_redirects=False)
    assert resp.status_code == 200
    assert "trip date is required" in resp.text
    assert db_session.execute(select(Claim)).scalars().first() is None


def test_mileage_requires_attestation(client, db_session, monkeypatch):
    """A mileage claim is out-of-pocket reimbursement, so the legacy /capture/mileage
    route must require the attestation (punch-list P3) exactly like the main capture
    form — no attested checkbox, nothing saved. Fails if the guard is removed."""
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    _mileage_category(db_session)
    resp = client.post("/capture/mileage",
                       data={"origin": "A", "destination": "B", "trip_date": "2026-03-12"},
                       follow_redirects=False)   # no attested
    assert resp.status_code == 200
    assert "out-of-pocket declaration" in resp.text
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
                data={"attested": "yes", "items": json.dumps([
                    {"expense_type": "other", "total_amount": "20", "vendor": "Cafe"}])},
                follow_redirects=False)
    claim = db_session.execute(select(Claim)).scalars().one()

    # 2) add a mileage line to it from the review screen (declaration required)
    r = client.post(f"/claims/{claim.id}/mileage",
                    data={"origin": "A", "destination": "B", "trip_date": "2026-03-12",
                          "attested": "yes"},
                    follow_redirects=False)
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
    client.post("/capture/mileage",
                data={"origin": "A", "destination": "B", "trip_date": "2026-03-12",
                      "attested": "yes"},
                follow_redirects=False)
    claim = db_session.execute(select(Claim)).scalars().one()
    # This test approves as the SAME user who keyed the trip; quick-mileage now
    # records the maker, so clear it here — the SoD bite is pinned separately.
    claim.created_by_user_id = None
    db_session.commit()
    assert client.post(f"/api/claims/{claim.id}/approve").status_code == 200
    assert client.post(f"/api/claims/{claim.id}/release").status_code == 200
    # released → adding a line is rejected (re-renders review with the error)
    r = client.post(f"/claims/{claim.id}/mileage",
                    data={"origin": "C", "destination": "D", "trip_date": "2026-03-12"},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "cannot add a line" in r.text


def test_review_shows_route_map_for_mileage(client, db_session, monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    _mileage_category(db_session)
    client.post("/capture/mileage", data={
        "origin": "KL Sentral", "destination": "Cyberjaya", "trip_date": "2026-03-12",
        "attested": "yes",
    }, follow_redirects=False)
    claim = db_session.execute(select(Claim)).scalars().one()
    page = client.get(f"/claims/{claim.id}/review").text
    assert "LINE_MILEAGE" in page and "route-summary" in page
    assert "KL Sentral" in page


def test_capture_receipt_and_mileage_in_one_claim(client, db_session, monkeypatch):
    """Combined capture: a receipt AND a mileage trip submitted together = one claim
    with both lines."""
    import json
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    _mileage_category(db_session)
    r = client.post(
        "/capture",
        files=[("files", ("r.png", b"\x89PNG\r\n fake", "image/png"))],
        data={
            "attested": "yes",
            "items": json.dumps([
                {"expense_type": "other", "total_amount": "20", "vendor": "Cafe"}]),
            "mileage": json.dumps([
                {"origin": "A", "destination": "B", "waypoints": [],
                 "route_index": 0, "trip_date": "2026-03-12"}]),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    claim = db_session.execute(select(Claim)).scalars().one()
    lines = db_session.execute(
        select(ClaimLine).filter_by(claim_id=claim.id)).scalars().all()
    assert sorted(l.expense_type for l in lines) == ["mileage", "other"]
    mil = next(l for l in lines if l.expense_type == "mileage")
    assert mil.quantity == Decimal("38.200") and mil.doc_date == "2026-03-12"


def test_capture_mileage_only_no_receipt(client, db_session, monkeypatch):
    """A mileage-only claim can go through the combined /capture (no receipt files)."""
    import json
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    _mileage_category(db_session)
    r = client.post("/capture", data={
        "attested": "yes",
        "items": "[]",
        "mileage": json.dumps([
            {"origin": "A", "destination": "B", "route_index": 0,
             "trip_date": "2026-03-12"}]),
    }, follow_redirects=False)
    assert r.status_code == 303
    line = db_session.execute(select(ClaimLine)).scalar_one()
    assert line.expense_type == "mileage"


def test_capture_mileage_missing_date_skipped(client, db_session, monkeypatch):
    """A mileage spec without a trip date is rejected (date compulsory)."""
    import json
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    _mileage_category(db_session)
    r = client.post("/capture", data={
        "attested": "yes",
        "items": "[]",
        "mileage": json.dumps([{"origin": "A", "destination": "B", "route_index": 0}]),
    }, follow_redirects=False)
    assert r.status_code == 200            # re-rendered with the error, no claim
    assert "trip date is required" in r.text
    assert db_session.execute(select(Claim)).scalars().first() is None
