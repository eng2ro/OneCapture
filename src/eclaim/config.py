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

# The committed placeholder signing secret. Safe for local dev only; the app
# refuses to run in production while jwt_secret is still this value.
DEFAULT_JWT_SECRET = "dev-only-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Deployment environment. "dev" (default) keeps the local passwordless login
    # and placeholder secret working; set ENVIRONMENT=production for deployments,
    # which hardens auth (see ``dev_auth_allowed`` / ``assert_production_safe``).
    environment: str = "dev"

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
    jwt_secret: str = DEFAULT_JWT_SECRET
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

    # Google Maps Platform — mileage claims (Directions for distance + Places +
    # Maps JS). ``google_maps_api_key`` is used SERVER-SIDE (Directions REST, key
    # hidden) and is the authoritative distance source for reimbursement.
    # ``google_maps_browser_key`` is the key injected into the page for Maps JS +
    # Places (necessarily visible to the browser — restrict it by HTTP referrer in
    # Cloud Console); falls back to ``google_maps_api_key`` if unset.
    google_maps_api_key: str = ""
    google_maps_browser_key: str = ""
    # Per-km reimbursement rate (MYR). A real client overrides this; sane default.
    mileage_rate_per_km: str = "0.60"

    # Spend-based EEIO fallback factor (kgCO2e per unit currency). Placeholder
    # value (decision D14); lives here so the carbon lead can revise it.
    spend_factor: str = "0.35"

    image_dir: Path = DEFAULT_IMAGE_DIR

    # Upload guards (blocker B7): the whole request body is buffered in memory
    # (UploadFile.read / request.form), so an unbounded upload OOMs the single
    # host. Cap the request body size and the number of files per capture. Both
    # are settings (Appendix B: configuration, not customization) — a firm with
    # heavy bulk/PDF batches can raise them. Default 50 MB comfortably fits a
    # multi-page PDF or a modest receipt ZIP while bounding worst-case memory.
    max_upload_mb: int = 50
    max_upload_files: int = 100

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    # Login brute-force throttle (HIGH): block after this many failed attempts per
    # IP or per email within the rolling window. Settings so a firm can tune them.
    login_max_attempts: int = 10
    login_window_seconds: int = 900   # 15 minutes

    # "Share gate" — an OUTER HTTP Basic Auth front door for TEMPORARILY exposing a
    # dev instance (e.g. through a tunnel) to a colleague, so a random visitor who
    # finds the public URL can't reach the passwordless app. INACTIVE unless BOTH are
    # set, so local dev is unaffected. A coarse gate on top of the app's own login —
    # NOT a substitute for real auth.
    share_gate_user: str = ""
    share_gate_pass: str = ""

    @property
    def share_gate_on(self) -> bool:
        return bool(self.share_gate_user and self.share_gate_pass)

    # Identity is single-firm for now; real auth (Entra ID) is a deferred seam.
    default_releaser: str = "system"

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in ("production", "prod")

    @property
    def dev_auth_allowed(self) -> bool:
        """Passwordless DevAuthProvider login is permitted only outside production.
        In production, login must go through a real credential/SSO provider."""
        return not self.is_production

    def assert_production_safe(self) -> None:
        """Fail fast at startup if a deployment is misconfigured in a way that
        would weaken auth. No-op in dev so local runs are unaffected."""
        if not self.is_production:
            return
        problems = []
        if self.jwt_secret == DEFAULT_JWT_SECRET:
            problems.append("JWT_SECRET is still the committed default — set a strong secret")
        if not self.session_cookie_secure:
            problems.append("SESSION_COOKIE_SECURE must be true in production")
        if problems:
            raise RuntimeError(
                "Insecure production configuration: " + "; ".join(problems)
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
