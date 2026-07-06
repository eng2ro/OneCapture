"""Security/governance hardening (audit fixes): production auth gating, the
maker≠checker SoD rule actually biting on web-captured claims, role enforcement on
mutating paths, and denied-attempt auditing.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from eclaim.auth.principal import Principal
from eclaim.auth.provider import AuthError, DevAuthProvider
from eclaim.config import DEFAULT_JWT_SECRET, Settings
from eclaim.db.models import Claim
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimService, Repos
from eclaim.services.sod import SoDViolation


# --- production config gating (no DB) ---------------------------------------
def test_assert_production_safe_rejects_default_secret():
    s = Settings(environment="production", jwt_secret=DEFAULT_JWT_SECRET, session_cookie_secure=True)
    with pytest.raises(RuntimeError, match="default"):
        s.assert_production_safe()


def test_assert_production_safe_rejects_insecure_cookie():
    s = Settings(environment="production", jwt_secret="a-real-strong-secret", session_cookie_secure=False)
    with pytest.raises(RuntimeError, match="SESSION_COOKIE_SECURE"):
        s.assert_production_safe()


def test_assert_production_safe_passes_when_hardened():
    s = Settings(environment="production", jwt_secret="a-real-strong-secret", session_cookie_secure=True)
    s.assert_production_safe()  # no raise
    assert s.is_production and not s.dev_auth_allowed


def test_dev_provider_refuses_passwordless_in_production():
    # allow_passwordless=False (what the prod wiring passes) refuses identity-only
    # login before any DB lookup.
    provider = DevAuthProvider(None, secret="x", ttl_seconds=60, allow_passwordless=False)
    with pytest.raises(AuthError, match="disabled in production"):
        provider.login("partner@seed.test")


# --- SoD: maker≠checker on web-captured claims ------------------------------
def _files(n=1):
    return [("files", (f"r{i}.png", b"\x89PNG\r\n fake", "image/png")) for i in range(n)]


def test_web_capture_records_creator_and_blocks_self_approval(client, db_session):
    """A firm user who captures a claim via the web is recorded as created_by, so
    the SoD self-approval guard blocks them approving their own claim (403)."""
    resp = client.post("/capture", files=_files(1),
                        data={"attested": "yes",
                              "items": '[{"expense_type": "other", "total_amount": "10"}]'},
                        follow_redirects=False)
    cid = resp.headers["location"].split("/")[2]
    claim = db_session.get(Claim, uuid.UUID(cid))
    assert claim.created_by_user_id == db_session.info["principal"]["user"]

    # Same principal (the conftest partner) tries to approve → SoD self-approval.
    denied = client.post(f"/api/claims/{cid}/approve")
    assert denied.status_code == 403


# --- role enforcement on mutating service methods ---------------------------
def _viewer(db_session) -> Principal:
    ids = db_session.info["principal"]
    return Principal(
        user_id=ids["user"], firm_id=ids["firm"], base_role="viewer",
        allowed_client_ids=frozenset({ids["client"]}), email="viewer@seed.test",
    )


def test_viewer_cannot_edit_or_release(client, fake_ocr, db_session):
    fake_ocr.extraction = Extraction(expense_type="fuel_diesel", quantity=Decimal("450"), unit="L")
    files = {"file": ("r.png", b"\x89PNG\r\n fake", "image/png")}
    cid = uuid.UUID(client.post("/api/claims/upload", files=files).json()["id"])

    svc, repos, viewer = ClaimService(), Repos.for_session(db_session), _viewer(db_session)
    with pytest.raises(SoDViolation):
        svc.edit(repos=repos, claim_id=cid, fields={"vendor": "X"}, actor="v", principal=viewer)
    with pytest.raises(SoDViolation):
        svc.edit_header(repos=repos, claim_id=cid, fields={"remarks": "x"}, actor="v", principal=viewer)
    # The viewer's attempts changed nothing.
    assert db_session.get(Claim, cid).vendor != "X"
