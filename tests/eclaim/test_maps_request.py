"""The Google Routes request body — pins the recurring "alternative routes
disappeared" regression (owner reports, latest 2026-07-09).

Google only computes alternative routes generously under
``routingPreference: TRAFFIC_AWARE_OPTIMAL``; under the cheap default it
silently returns ONE route for many trips (verified live: KL→Johor Bahru).
The route picker on the capture page therefore lives or dies by this request
shape — if these tests fail, the picker WILL vanish for real trips again.
"""

from __future__ import annotations

from eclaim.maps import _build_body


def test_alternatives_request_uses_traffic_aware_optimal():
    body = _build_body("KL", "JB", None, alternatives=True)
    assert body["computeAlternativeRoutes"] is True
    assert body["routingPreference"] == "TRAFFIC_AWARE_OPTIMAL"
    assert "intermediates" not in body


def test_waypoint_trip_sends_intermediates_and_no_alternatives():
    # Google rejects computeAlternativeRoutes together with intermediates.
    body = _build_body("KL", "JB", ["Seremban"], alternatives=False)
    assert body["intermediates"] == [{"address": "Seremban"}]
    assert "computeAlternativeRoutes" not in body
    assert "routingPreference" not in body


def test_single_route_call_stays_on_the_cheap_tier():
    # Only the alternatives path pays the higher-priced SKU.
    body = _build_body("KL", "JB", None, alternatives=False)
    assert "computeAlternativeRoutes" not in body
    assert "routingPreference" not in body
