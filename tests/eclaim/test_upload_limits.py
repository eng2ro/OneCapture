"""Upload guards (blocker B7): request-body byte cap + per-capture file count.

The byte cap is exercised against the real middleware (declared Content-Length AND
actual streamed bytes with no length); the file-count cap is exercised through the
real ``/capture`` handler.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from eclaim.api.limits import BodySizeLimitMiddleware


def _tiny_app(max_bytes: int) -> FastAPI:
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)

    @app.post("/echo")
    async def echo(request: Request) -> dict:
        body = await request.body()
        return {"len": len(body)}

    return app


def test_body_under_cap_passes():
    with TestClient(_tiny_app(100)) as c:
        r = c.post("/echo", content=b"x" * 50)
        assert r.status_code == 200
        assert r.json()["len"] == 50


def test_declared_length_over_cap_is_413():
    # httpx sets Content-Length for a bytes body → the fast path rejects it.
    with TestClient(_tiny_app(100)) as c:
        r = c.post("/echo", content=b"x" * 500)
        assert r.status_code == 413
        assert "too large" in r.text.lower()


def test_streamed_body_without_length_is_capped():
    """A body streamed with no Content-Length (chunked / lying client) must still be
    aborted once it crosses the cap — driven at the ASGI layer so we control the
    absence of a length header and the raw body frames."""
    mw = BodySizeLimitMiddleware(_should_not_run, max_bytes=100)
    scope = {"type": "http", "method": "POST", "path": "/x", "headers": []}  # no length
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"x" * 500, "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    async def reader(scope, rcv, snd):
        # Simulate a handler consuming the body — this is where the cap trips.
        await rcv()

    mw.app = reader
    asyncio.run(mw(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413


async def _should_not_run(scope, receive, send):  # pragma: no cover - placeholder
    raise AssertionError("app should not be reached")


# --------------------------------------------------------------------------- #
# Body cap vs ZIP budget invariant (punch-list P2)
# --------------------------------------------------------------------------- #
def test_body_cap_covers_zip_budget():
    """The request-body cap must be >= the ZIP expansion budget, or a legitimate
    full-budget receipt ZIP (mostly barely-compressible images) is 413'd before it
    ever reaches ZIP expansion. Fails if max_upload_mb is dropped below the ZIP
    total budget again — the exact regression P2 fixed."""
    from eclaim.config import get_settings
    from eclaim.services.ingestion import _ZIP_MAX_TOTAL_BYTES

    assert get_settings().max_upload_bytes >= _ZIP_MAX_TOTAL_BYTES, (
        "request body cap is below the documented ZIP budget — bulk ZIPs will 413"
    )


# --------------------------------------------------------------------------- #
# File-count cap on /capture
# --------------------------------------------------------------------------- #
def test_capture_rejects_too_many_files(client, monkeypatch):
    from eclaim.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_files", 1)
    files = [
        ("files", ("a.jpg", b"\xff\xd8\xff", "image/jpeg")),
        ("files", ("b.jpg", b"\xff\xd8\xff", "image/jpeg")),
    ]
    resp = client.post("/capture", files=files, follow_redirects=False)
    assert resp.status_code == 200          # re-render with the error, not a redirect
    assert "Too many files" in resp.text
    assert "at most 1" in resp.text
