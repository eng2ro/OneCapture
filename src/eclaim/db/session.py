"""Engine + session factory, built from :mod:`eclaim.config`.

A single module-level engine is created lazily on first use so importing the
package never requires a reachable database (tests build their own engine
against ``TEST_DATABASE_URL``).
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def app_database_url() -> str:
    """The DSN the app connects with — the non-superuser ``onecapture_app`` role,
    so Row-Level Security is actually enforced.

    Fail-closed: if ``APP_DATABASE_URL`` is unset we refuse to fall back to the
    admin DSN, because connecting as the table owner/superuser silently bypasses
    every RLS policy (the exact untested-isolation trap). Set
    ``OC_ALLOW_ADMIN_FALLBACK=1`` to opt into the admin DSN for local,
    non-isolation poking only — never in a deployed environment."""
    s = get_settings()
    if s.app_database_url:
        return s.app_database_url
    if os.environ.get("OC_ALLOW_ADMIN_FALLBACK") == "1":
        return s.database_url
    raise RuntimeError(
        "refusing to start: APP_DATABASE_URL unset — would connect as a "
        "privileged role and bypass RLS. Set APP_DATABASE_URL to the "
        "onecapture_app DSN, or OC_ALLOW_ADMIN_FALLBACK=1 to override locally."
    )


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(app_database_url(), future=True, pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _Session


def get_session() -> Session:
    """FastAPI dependency: yields a session, always closed."""
    db = get_sessionmaker()()
    try:
        yield db
    finally:
        db.close()
