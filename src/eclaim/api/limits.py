"""Request body size limit (blocker B7).

The app buffers whole request bodies in memory — ``UploadFile.read()`` in the
upload/capture handlers, and ``request.form()`` (CSRF guard + multipart parse) —
so an unbounded upload OOMs the single host. This pure-ASGI middleware caps the
body BEFORE any handler reads it, two ways:

1. **Declared length** — a ``Content-Length`` over the cap is rejected with 413
   before a single body byte is read (the common case; browsers and HTTP clients
   always send it for uploads).
2. **Actual bytes streamed** — a body with no/understated ``Content-Length``
   (chunked, or a lying client) is counted as it arrives and aborted the moment it
   crosses the cap, so the fast path can't be bypassed to still exhaust memory.

It sits outside the framework's body handling, so the cap holds regardless of how
a given endpoint consumes the body. A reverse proxy should ALSO cap upstream
(deferred B8); this makes the app safe on its own meanwhile.
"""

from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _BodyTooLarge(Exception):
    """Raised from the wrapped ``receive`` once the streamed body crosses the cap."""


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # (1) Fast path: reject an over-cap declared length before reading the body.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
                break

        # (2) Enforce the real byte count for chunked / absent / understated lengths.
        received = 0
        exceeded = False
        forwarded_start = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message: Message) -> None:
            # Once the cap is exceeded, suppress whatever the app emits. A framework
            # body parser (FastAPI/Starlette multipart) catches our _BodyTooLarge and
            # turns it into its OWN 400 "error parsing the body" response; if we let
            # that through, the client sees a misleading 400 instead of the intended
            # 413. We swallow it and synthesize the 413 after the app unwinds.
            nonlocal forwarded_start
            if exceeded:
                return
            if message["type"] == "http.response.start":
                forwarded_start = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            pass   # the app let our signal propagate; fall through to the 413 below
        # Send the 413 iff the cap tripped and nothing real was forwarded to the
        # client yet (the guarded_send above ensures a suppressed 400 never counts).
        if exceeded and not forwarded_start:
            await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        mb = self.max_bytes / (1024 * 1024)
        response = PlainTextResponse(
            f"Upload too large — the limit is {mb:.0f} MB.", status_code=413
        )
        await response(scope, receive, send)
