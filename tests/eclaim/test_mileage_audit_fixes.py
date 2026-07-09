"""Fixes from the mileage-surfaces audit (owner escalation 2026-07-09: "audit
all related module n solve" — one bug report must sweep every surface of the
feature, not just the page it was seen on).
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from eclaim.db.models import Claim, ClaimLine
from eclaim.maps import MapError, RouteResult, UnconfiguredDirectionsProvider
from eclaim.ocr.base import Extraction


class _FakeDirections:
    def route(self, origin, destination, waypoints=None):
        return RouteResult(distance_km=Decimal("38.200"), polyline="p",
                           legs=[{"from": origin, "to": destination, "km": "38.200"}])

    def routes(self, origin, destination, waypoints=None):
        return [self.route(origin, destination, waypoints)]


class _BrokenDirections:
    def route(self, origin, destination, waypoints=None):
        raise MapError("no route found")

    routes = route


class _ZeroKmDirections:
    def routes(self, origin, destination, waypoints=None):
        return [RouteResult(distance_km=Decimal("0.000"), polyline="p", legs=[])]

    route = lambda self, *a, **k: self.routes(*a, **k)[0]  # noqa: E731


# --- SoD: the quick mileage endpoint now records its maker -------------------
def test_quick_mileage_claim_cannot_be_self_approved(client, db_session, monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    r = client.post("/capture/mileage", data={
        "origin": "KL", "destination": "Seremban", "trip_date": "2026-07-09",
        "attested": "yes", "vehicle_id": "",
    }, follow_redirects=False)
    cid = r.headers["location"].split("/claims/")[1].split("/")[0]
    claim = db_session.get(Claim, uuid.UUID(cid))
    assert claim.created_by_user_id == db_session.info["principal"]["user"]
    # Maker ≠ checker: the same principal may NOT approve their own trip.
    assert client.post(f"/api/claims/{cid}/approve").status_code == 403


# --- attestation: the review modal's declaration is enforced server-side -----
def test_review_modal_mileage_requires_the_declaration(client, db_session, fake_ocr,
                                                       monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _FakeDirections())
    files = {"file": ("r.png", b"\x89PNG att-1", "image/png")}
    cid = client.post("/api/claims/upload", files=files,
                      data={"attested": "true"}).json()["id"]

    data = {"origin": "A", "destination": "B", "trip_date": "2026-07-09"}
    r = client.post(f"/claims/{cid}/mileage", data=data, follow_redirects=False)
    assert r.status_code == 200 and "confirm the declaration" in r.text
    assert all(l.expense_type != "mileage" for l in db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))).scalars())

    # With the tick the line files AND the claim's stamp is refreshed to the adder.
    r = client.post(f"/claims/{cid}/mileage", data=data | {"attested": "yes"},
                    follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    claim = db_session.get(Claim, uuid.UUID(cid))
    assert claim.attested_by is not None and claim.attested_at is not None


# --- zero-km guard (shared by every surface via add_mileage_line) ------------
def test_zero_km_trip_is_refused_not_filed(client, db_session, monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _ZeroKmDirections())
    r = client.post("/capture/mileage", data={
        "origin": "Same Place", "destination": "Same Place",
        "trip_date": "2026-07-09", "attested": "yes", "vehicle_id": "",
    }, follow_redirects=False)
    assert r.status_code == 200 and "0 km" in r.text
    assert db_session.execute(select(Claim)).scalars().first() is None


# --- partial capture failure is said out loud, not swallowed -----------------
def test_failed_mileage_trip_in_a_combined_capture_is_reported(client, db_session,
                                                               fake_ocr, monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(deps, "get_directions", lambda: _BrokenDirections())
    fake_ocr.extraction = Extraction(vendor="Kedai", total_amount=Decimal("10.00"),
                                     expense_type="other")
    r = client.post("/capture",
                    files=[("files", ("r.png", b"\x89PNG mix", "image/png"))],
                    data={"attested": "yes",
                          "items": json.dumps([{"expense_type": "other",
                                                "total_amount": "10"}]),
                          "mileage": json.dumps([{"origin": "A", "destination": "B",
                                                  "trip_date": "2026-07-09"}])},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "err=" in r.headers["location"]            # the receipt filed, the trip failed
    page = client.get(r.headers["location"]).text
    assert "Some items could not be added" in page    # banner on the review screen


# --- unconfigured Maps key: capture must not 500 -----------------------------
def test_receipts_only_capture_works_without_a_maps_key(client, db_session, fake_ocr,
                                                        monkeypatch):
    from eclaim.api import deps
    monkeypatch.setattr(
        deps, "get_settings",
        lambda: SimpleNamespace(google_maps_api_key="",
                                mileage_rate_per_km="0.60"),
    )
    fake_ocr.extraction = Extraction(vendor="Kedai", total_amount=Decimal("10.00"),
                                     expense_type="other")
    r = client.post("/capture",
                    files=[("files", ("r.png", b"\x89PNG nokey", "image/png"))],
                    data={"attested": "yes",
                          "items": json.dumps([{"expense_type": "other",
                                                "total_amount": "10"}])},
                    follow_redirects=False)
    assert r.status_code == 303 and "/review" in r.headers["location"]


def test_unconfigured_provider_raises_maperror_only_on_use():
    p = UnconfiguredDirectionsProvider()                  # constructing never throws
    for call in (p.route, p.routes):
        try:
            call("A", "B")
            raise AssertionError("expected MapError")
        except MapError as exc:
            assert "not configured" in str(exc)


# --- modal parity bits pinned in the rendered page ---------------------------
def test_review_page_modal_has_declaration_rate_and_required_fields(client, fake_ocr):
    files = {"file": ("r.png", b"\x89PNG modal", "image/png")}
    cid = client.post("/api/claims/upload", files=files,
                      data={"attested": "true"}).json()["id"]
    page = client.get(f"/claims/{cid}/review").text
    assert 'name="attested"' in page                     # declaration checkbox
    assert 'id="am-add" disabled' in page                # no submit before a route
    assert "AM_RATE" in page                             # RM pricing wired
    assert 'name="origin" id="am-from" autocomplete="off" placeholder="Start location" required' in page
