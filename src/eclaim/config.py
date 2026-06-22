"""e-Claim runtime configuration (pydantic-settings, reads ``.env``).

``DATABASE_URL`` / ``TEST_DATABASE_URL`` are SQLAlchemy URLs
(``postgresql+psycopg://...``). ``ANTHROPIC_API_KEY`` is only needed for live
OCR — tests mock the provider and never read it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Local-disk image store (object storage is a deferred seam).
DEFAULT_IMAGE_DIR = Path("data/eclaim_images")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Admin/owner DSN — runs Alembic migrations (table owner, may bypass RLS).
    database_url: str = "postgresql+psycopg://localhost:5432/onecapture"
    test_database_url: str = "postgresql+psycopg://localhost:5432/onecapture_test"

    # App DSN — the non-superuser ``onecapture_app`` role the app AND tests
    # connect as, so Row-Level Security is actually enforced (owners/superusers
    # bypass RLS). Falls back to the admin DSN only if unset (RLS won't bite).
    app_database_url: str = ""
    app_test_database_url: str = ""

    # DevAuthProvider token signing secret (HMAC). Real Entra ID is a seam. The
    # browser session cookie carries this same signed token — no separate secret.
    jwt_secret: str = "dev-only-change-me"
    jwt_ttl_seconds: int = 3600
    # Session cookie Secure flag: True (HTTPS-only) for deployments; set
    # SESSION_COOKIE_SECURE=false for local http dev so the cookie rides over http.
    session_cookie_secure: bool = True

    # CarbonNext service identity (IR-6 "company_dataentry"). One token for all
    # companies; the per-batch destination is client.carbonnext_company_id.
    carbonnext_api_url: str = "https://carbonnext.example/api"
    carbonnext_service_token: str = ""

    anthropic_api_key: str = ""
    ocr_model: str = "claude-sonnet-4-6"

    # Spend-based EEIO fallback factor (kgCO2e per unit currency). Placeholder
    # value (decision D14); lives here so the carbon lead can revise it.
    spend_factor: str = "0.35"

    image_dir: Path = DEFAULT_IMAGE_DIR

    # Identity is single-firm for now; real auth (Entra ID) is a deferred seam.
    default_releaser: str = "system"


@lru_cache
def get_settings() -> Settings:
    return Settings()
