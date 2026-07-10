"""FastAPI application factory: JSON API + server-rendered web UI."""

from __future__ import annotations

import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from erpsync.review.routes import api_router as erpsync_api_router
from erpsync.review.routes import web_router as erpsync_web_router

from ..auth.routes import router as auth_router
from ..config import get_settings
from ..web.routes import WEB_DIR, router as web_router
from .deps import CsrfError, NeedsLogin, WebForbidden
from .limits import BodySizeLimitMiddleware
from .routes import router as api_router


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Run the in-process ingestion worker for the app's lifetime. Disabled with
    ``OC_DISABLE_INGEST_WORKER=1`` (set in tests, which drive ingestion directly)."""
    worker = None
    if os.environ.get("OC_DISABLE_INGEST_WORKER") != "1":
        from ..ingest.worker import Worker

        worker = Worker()
        worker.start()
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()
            logging.getLogger(__name__).info("ingestion worker stopped")


def create_app() -> FastAPI:
    # Fail fast if a production deployment is misconfigured (default secret,
    # insecure cookie). No-op in dev, so local runs are unaffected.
    settings = get_settings()
    settings.assert_production_safe()
    app = FastAPI(title="OneCapture", version="0.2.0", lifespan=_lifespan)
    # Per-app login throttle (HIGH). App-scoped (not a module global) so each test's
    # fresh app starts with a clean limiter — no cross-test bleed.
    from ..auth.ratelimit import LoginRateLimiter

    app.state.login_limiter = LoginRateLimiter(
        max_attempts=settings.login_max_attempts,
        window_seconds=settings.login_window_seconds,
    )
    app.include_router(auth_router)
    app.include_router(api_router)
    app.include_router(web_router)
    # ERP Sync review (FR-S5): shares the same auth/RLS plumbing + static styling.
    app.include_router(erpsync_api_router)
    app.include_router(erpsync_web_router)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    # Cap the request body before any handler buffers it in memory (blocker B7).
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=get_settings().max_upload_bytes)

    # Server-rendered HTML is always live (auth-gated, per-request state) and its
    # behaviour ships as INLINE script — so a cached page silently runs stale JS.
    # Tell the browser never to store HTML; static assets keep their own caching.
    @app.middleware("http")
    async def _no_store_html(request: Request, call_next):
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # Optional OUTER front door for sharing a dev instance over a tunnel: when a
    # share gate is configured, every request must carry the shared HTTP Basic
    # credential, else 401. No-op when unset (normal local dev). This only keeps
    # strangers off the URL; the app's own login still applies behind it.
    @app.middleware("http")
    async def _share_gate(request: Request, call_next):
        settings = get_settings()
        if settings.share_gate_on:
            header = request.headers.get("authorization", "")
            authorized = False
            if header.startswith("Basic "):
                try:
                    user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                    authorized = secrets.compare_digest(user, settings.share_gate_user) and \
                        secrets.compare_digest(pw, settings.share_gate_pass)
                except Exception:
                    authorized = False
            if not authorized:
                return Response(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="OneCapture (shared test)"'},
                )
        return await call_next(request)

    # A web page reached without a session cookie redirects to login (the API's
    # bearer paths raise 401 instead — they never raise NeedsLogin).
    @app.exception_handler(NeedsLogin)
    async def _redirect_to_login(request: Request, exc: NeedsLogin) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    # A logged-in user who lacks firm scope reached an admin page: render a friendly
    # in-shell 'no access' page (status 403) instead of the API's bare JSON, so the
    # browser shows a readable message with a way back. The nav context processor is
    # defensively wrapped, so the sidebar still renders around it.
    @app.exception_handler(WebForbidden)
    async def _forbidden(request: Request, exc: WebForbidden) -> Response:
        from ..web.routes import templates

        return templates.TemplateResponse(
            request, "_forbidden.html", {"detail": str(exc) or None}, status_code=403
        )

    # A cookie-authenticated state-changing request without a valid CSRF token:
    # blocked as a 403 with a readable page (a forged cross-site POST, or a stale
    # form after the session rotated). The browser can reload and retry.
    @app.exception_handler(CsrfError)
    async def _csrf_failed(request: Request, exc: CsrfError) -> Response:
        from ..web.routes import templates

        return templates.TemplateResponse(
            request,
            "_forbidden.html",
            {
                "heading": "Please reload and try again",
                "detail": "Your security token was missing or has expired. "
                "Reload the page and try again.",
            },
            status_code=403,
        )

    # A browser hitting a missing/mistyped record (404) or a malformed URL (422)
    # gets a friendly in-shell page with a way back, not raw JSON. Bearer API
    # paths (/api/*) and non-HTML clients keep the machine-readable JSON.
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def _wants_html(request: Request) -> bool:
        return (
            not request.url.path.startswith("/api")
            and "text/html" in request.headers.get("accept", "")
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404 and _wants_html(request):
            from ..web.routes import templates

            return templates.TemplateResponse(
                request, "_notice.html",
                {
                    "topbar": "Not found", "icon": "ti-search-off",
                    "heading": "We couldn't find that",
                    "detail": "That page or record doesn't exist — it may have been "
                    "removed, or the link is mistyped. Go back to All claims to continue.",
                },
                status_code=404,
            )
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError) -> Response:
        if _wants_html(request):
            from ..web.routes import templates

            return templates.TemplateResponse(
                request, "_notice.html",
                {
                    "topbar": "Invalid link", "icon": "ti-alert-triangle",
                    "heading": "Something in that link looks wrong",
                    "detail": "The address contains an invalid value. Go back to All "
                    "claims and try again.",
                },
                status_code=422,
            )
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    return app


app = create_app()
