"""Maps / directions seam for mileage claims.

Server-side distance is the AUTHORITATIVE figure for reimbursement — never trust a
client-submitted km. :class:`GoogleDirectionsProvider` calls the Google **Routes
API** (``computeRoutes``, the current non-legacy product; key hidden on the
server). The :class:`DirectionsProvider` protocol lets tests inject a fake and lets
us swap providers later.

Distances use ``Decimal`` (km, 3 dp) so the reimbursement amount never float-drifts.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol


class MapError(RuntimeError):
    """Raised when a route can't be computed (no key, transport, or no route)."""


_STATIC_MAP_URL = "https://maps.googleapis.com/maps/api/staticmap"


def fetch_static_map(
    api_key: str,
    *,
    polyline: str | None = None,
    markers: list[str] | None = None,
    size: str = "640x360",
    timeout: float = 8.0,
) -> bytes:
    """Fetch a Google **Static Maps** image SERVER-SIDE and return the PNG bytes.

    Keeping the call on the server means the API key is never exposed to the browser
    and any IP allow-listing on the key still matches — so the route map shows with
    only ``GOOGLE_MAPS_API_KEY`` configured (no separate browser key needed). The
    route is drawn from the encoded ``polyline``; ``markers`` (already-formatted
    Static Maps marker specs) pin the endpoints. Raises :class:`MapError` if the key
    is missing or Google rejects the request (e.g. the Maps Static API isn't enabled
    on the key)."""
    if not api_key:
        raise MapError("Google Maps API key is not configured")
    params: list[tuple[str, str]] = [("size", size), ("scale", "2"), ("key", api_key)]
    if polyline:
        params.append(("path", f"color:0x2563ebff|weight:4|enc:{polyline}"))
    for m in markers or []:
        params.append(("markers", m))
    url = _STATIC_MAP_URL + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = str(exc)
        raise MapError(f"static map request failed: {detail}") from exc
    except Exception as exc:  # transport
        raise MapError(f"static map request failed: {exc}") from exc


@dataclass(frozen=True)
class RouteResult:
    distance_km: Decimal
    polyline: str | None = None
    legs: list[dict] = field(default_factory=list)  # [{from, to, km}]
    description: str | None = None  # Google's "via …" label, to name alternatives

    @property
    def stops(self) -> int:
        return len(self.legs) + 1 if self.legs else 0


class DirectionsProvider(Protocol):
    def route(
        self, origin: str, destination: str, waypoints: list[str] | None = None
    ) -> RouteResult: ...

    def routes(
        self, origin: str, destination: str, waypoints: list[str] | None = None
    ) -> list[RouteResult]: ...


class GoogleDirectionsProvider:
    """Google **Routes API** (``computeRoutes``). The API key stays server-side; the
    browser uses a separate referrer-restricted key for the map display only.

    Routes API is Google's current product (the legacy Directions API is
    deprecated). It is a POST with a JSON body and an ``X-Goog-FieldMask`` selecting
    only the fields we need (distance + encoded polyline + per-leg distance)."""

    _URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
    _FIELDS = (
        "routes.distanceMeters,routes.polyline.encodedPolyline,"
        "routes.legs.distanceMeters,routes.description"
    )

    def __init__(self, api_key: str, *, timeout: float = 8.0) -> None:
        if not api_key:
            raise MapError("Google Maps API key is not configured")
        self._key = api_key
        self._timeout = timeout

    def route(
        self, origin: str, destination: str, waypoints: list[str] | None = None
    ) -> RouteResult:
        """The single recommended route (Google's ``routes[0]``)."""
        data = self._call(origin, destination, waypoints, alternatives=False)
        return _parse_routes(data, origin, destination, waypoints or [])

    def routes(
        self, origin: str, destination: str, waypoints: list[str] | None = None
    ) -> list[RouteResult]:
        """The recommended route plus alternatives, ``routes[0]`` first. Google only
        returns alternatives for a DIRECT trip — with intermediate waypoints this
        yields the single computed route as a one-element list."""
        data = self._call(origin, destination, waypoints, alternatives=not waypoints)
        return _parse_route_list(data, origin, destination, waypoints or [])

    def _call(
        self,
        origin: str,
        destination: str,
        waypoints: list[str] | None,
        *,
        alternatives: bool,
    ) -> dict:
        body: dict = {
            "origin": {"address": origin},
            "destination": {"address": destination},
            "travelMode": "DRIVE",
            "units": "METRIC",
            "regionCode": "MY",
        }
        if waypoints:
            body["intermediates"] = [{"address": w} for w in waypoints]
        elif alternatives:
            body["computeAlternativeRoutes"] = True  # only valid without intermediates
        req = urllib.request.Request(
            self._URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self._key,
                "X-Goog-FieldMask": self._FIELDS,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Routes API returns a JSON error body with a human message.
            try:
                msg = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", str(exc))
            except Exception:
                msg = str(exc)
            raise MapError(f"directions request failed: {msg}") from exc
        except Exception as exc:  # transport / parse
            raise MapError(f"directions request failed: {exc}") from exc


def _parse_route_list(
    data: dict, origin: str, destination: str, waypoints: list[str]
) -> list[RouteResult]:
    """Parse every route in a ``computeRoutes`` response, recommended (``routes[0]``)
    first. Raises :class:`MapError` when no route was found."""
    routes = data.get("routes") or []
    if not routes:
        raise MapError("no route found")
    return [_parse_one(r, origin, destination, waypoints) for r in routes]


def _parse_routes(
    data: dict, origin: str, destination: str, waypoints: list[str]
) -> RouteResult:
    """Parse the recommended route (``routes[0]``) into a :class:`RouteResult`."""
    return _parse_route_list(data, origin, destination, waypoints)[0]


def _parse_one(
    route: dict, origin: str, destination: str, waypoints: list[str]
) -> RouteResult:
    km = (Decimal(int(route.get("distanceMeters", 0))) / Decimal(1000)).quantize(Decimal("0.001"))
    stop_names = [origin, *waypoints, destination]
    legs = route.get("legs", [])
    leg_rows = [
        {
            "from": stop_names[i] if i < len(stop_names) else None,
            "to": stop_names[i + 1] if i + 1 < len(stop_names) else None,
            "km": str((Decimal(int(leg.get("distanceMeters", 0))) / 1000).quantize(Decimal("0.001"))),
        }
        for i, leg in enumerate(legs)
    ]
    return RouteResult(
        distance_km=km,
        polyline=(route.get("polyline") or {}).get("encodedPolyline"),
        legs=leg_rows,
        description=route.get("description") or None,
    )
