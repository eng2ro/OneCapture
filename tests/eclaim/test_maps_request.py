"""The Google Routes request body — pins the recurring "alternative routes
disappeared" regression (owner reports, latest 2026-07-09).

Google only computes alternative routes generously under
``routingPreference: TRAFFIC_AWARE_OPTIMAL``; under the cheap default it
silently returns ONE route for many trips (verified live: KL→Johor Bahru).
The route picker on the capture page therefore lives or dies by this request
shape — if these tests fail, the picker WILL vanish for real trips again.
"""

from __future__ import annotations

import pytest

from eclaim.maps import GoogleDirectionsProvider, MapError, _build_body


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


# --- empty-answer retry ladder (owner report 2026-07-09: the review modal
# dead-ended on "no route found" for a plainly routable trip) -----------------
class _ScriptedProvider(GoogleDirectionsProvider):
    """Overrides the HTTP call; returns the scripted responses in order."""

    def __init__(self, responses):
        super().__init__("test-key")
        self.responses, self.calls = list(responses), []

    def _call(self, origin, destination, waypoints, *, alternatives):
        self.calls.append(alternatives)
        return self.responses.pop(0)


def test_empty_alternatives_answer_retries_on_the_plain_tier():
    one_route = {"routes": [{"distanceMeters": 66000, "legs": []}]}
    p = _ScriptedProvider([{}, one_route])          # optimal empty → plain has it
    routes = p.routes("KL", "Seremban")
    assert len(routes) == 1
    assert p.calls == [True, False]                 # retried without alternatives


def test_route_still_missing_after_retry_gives_a_helpful_message():
    p = _ScriptedProvider([{}, {}])
    with pytest.raises(MapError, match="suggestion list"):
        p.routes("nowhere", "nowhere else")
    assert p.calls == [True, False]


def test_waypoint_trip_does_not_retry():
    # With intermediates there is no second tier to try — one call, clear error.
    p = _ScriptedProvider([{}])
    with pytest.raises(MapError, match="suggestion list"):
        p.routes("KL", "JB", ["Melaka"])
    assert p.calls == [False]
