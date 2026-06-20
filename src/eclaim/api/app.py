"""FastAPI application factory: JSON API + server-rendered web UI."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..auth.routes import router as auth_router
from ..web.routes import WEB_DIR, router as web_router
from .routes import router as api_router


def create_app() -> FastAPI:
    app = FastAPI(title="OneCapture e-Claim", version="0.2.0")
    app.include_router(auth_router)
    app.include_router(api_router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    return app


app = create_app()
