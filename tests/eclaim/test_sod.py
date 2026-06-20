"""Separation-of-duties + authority + grant-scope enforcement (spec §8).

Runs under the **real principal path**: the unprivileged ``onecapture_app``
connection with per-request tenant context (the ``db_session`` fixture), real
minted tokens, and the real ``get_principal`` resolution (no Principal override) —
so authority limits and grants are loaded from seeded ``app_user`` /
``user_client_grant`` rows, not hand-fed.

Each guard is tested on **both sides** (reject AND allow) so we know it is precise,
not blanket, and — for submitter≠approver — at **both layers**: the service/API
(clean ``SoDViolation`` → 403) and the database CHECK (``ck_claim_sod``).

Seeding note: seeds are committed (savepoint release) before each request so a
rejected request's rollback can't undo them; the fixture's outer transaction still
rolls the whole test back at teardown.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from eclaim.api import deps
from eclaim.api.app import create_app
from eclaim.auth import tokens
from eclaim.auth.principal import build_principal
from eclaim.config import get_settings
from eclaim.db.models import AppUser, Claim, UserClientGrant
from eclaim.services.sod import SoDViolation, check_can_approve


# --------------------------------------------------------------------------- #
# Seeded cast of firm users (all in the default firm, under firm context)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SoDWorld:
    firm: uuid.UUID
    client: uuid.UUID
    manager: uuid.UUID      # firm-scoped, no limit — claim creator
    manager2: uuid.UUID     # firm-scoped, no limit — a *different* approver
    capped: uuid.UUID       # approver, granted, authority_limit = 1000
    approver: uuid.UUID     # approver, granted, no limit
    ungranted: uuid.UUID    # approver, NO grant to the client
    viewer: uuid.UUID       # viewer, granted


@pytest.fixture
def sod_world(db_session) -> SoDWorld:
    ids = db_session.info["principal"]
    firm, client = ids["firm"], ids["client"]

    def user(email: str, role: str, limit: Decimal | None = None) -> AppUser:
        u = AppUser(
            firm_id=firm, email=email, display_name=email,
            base_role=role, authority_limit=limit,
        )
        db_session.add(u)
        db_session.flush()
        return u

    manager = user("sod-manager@seed.test", "manager")
    manager2 = user("sod-manager2@seed.test", "manager")
    capped = user("sod-capped@seed.test", "approver", Decimal("1000.00"))
    approver = user("sod-approver@seed.test", "approver")
    ungranted = user("sod-ungranted@seed.test", "approver")
    viewer = user("sod-viewer@seed.test", "viewer")

    for u in (capped, approver, viewer):  # ungranted gets none, on purpose
        db_session.add(UserClientGrant(firm_id=firm, user_id=u.id, client_id=client))
    db_session.flush()
    db_session.commit()  # protect the seed from a rejected request's rollback

    return SoDWorld(
        firm=firm, client=client,
        manager=manager.id, manager2=manager2.id, capped=capped.id,
        approver=approver.id, ungranted=ungranted.id, viewer=viewer.id,
    )


def _make_claim(db_session, world: SoDWorld, *, created_by, amount=Decimal("100.00")) -> Claim:
    """An in-review claim on the default client, stamped with its creator and
    amount. Committed so a later rejected request's rollback leaves it intact."""
    claim = Claim(
        firm_id=world.firm, client_id=world.client,
        created_by_user_id=created_by,
        image_path="/x/sod.png", image_sha256=uuid.uuid4().hex,
        total_amount=amount, status="in_review",
    )
    db_session.add(claim)
    db_session.flush()
    db_session.commit()
    return claim


# --------------------------------------------------------------------------- #
# Real-principal API client (token → real get_principal, no override)
# --------------------------------------------------------------------------- #
@pytest.fixture
def api(db_session):
    from fastapi.testclient import TestClient

    def _override_db():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _token(world: SoDWorld, user_id: uuid.UUID, role: str) -> str:
    return tokens.mint(
        {"user_id": str(user_id), "firm_id": str(world.firm), "base_role": role},
        secret=get_settings().jwt_secret, ttl_seconds=300,
    )


def _approve(api, claim_id: uuid.UUID, token: str):
    return api.post(
        f"/api/claims/{claim_id}/approve", headers={"Authorization": f"Bearer {token}"}
    )


# =========================================================================== #
# 1. Submitter ≠ approver — DB CHECK layer (ck_claim_sod)
# =========================================================================== #
def test_db_check_rejects_approver_equals_creator(db_session, sod_world):
    """The DB CHECK refuses approved_by == created_by, even if the service is
    bypassed entirely."""
    claim = _make_claim(db_session, sod_world, created_by=sod_world.manager)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            claim.approved_by_user_id = sod_world.manager  # == creator
            db_session.flush()


def test_db_check_allows_approver_distinct_from_creator(db_session, sod_world):
    """A distinct approver satisfies the CHECK — it gates self-approval only,
    not approval itself."""
    claim = _make_claim(db_session, sod_world, created_by=sod_world.manager)
    claim.approved_by_user_id = sod_world.manager2  # != creator
    db_session.flush()  # no IntegrityError
    assert claim.approved_by_user_id == sod_world.manager2


# =========================================================================== #
# 2. Submitter ≠ approver — service/API layer (clean SoDViolation → 403)
# =========================================================================== #
def test_api_rejects_self_approval(db_session, sod_world, api):
    """The creator approving their own claim is rejected before the DB, as a
    clean 403 — not a raw IntegrityError."""
    claim = _make_claim(db_session, sod_world, created_by=sod_world.manager)
    resp = _approve(api, claim.id, _token(sod_world, sod_world.manager, "manager"))
    assert resp.status_code == 403
    assert "cannot approve" in resp.json()["detail"].lower()
    db_session.refresh(claim)
    assert claim.status == "in_review" and claim.approved_by_user_id is None


def test_api_allows_approval_by_different_user(db_session, sod_world, api):
    claim = _make_claim(db_session, sod_world, created_by=sod_world.manager)
    resp = _approve(api, claim.id, _token(sod_world, sod_world.manager2, "manager"))
    assert resp.status_code == 200
    db_session.refresh(claim)
    assert claim.status == "approved" and claim.approved_by_user_id == sod_world.manager2


# =========================================================================== #
# 3. Authority limit — reject above, allow at/below (service/API, dynamic)
# =========================================================================== #
def test_api_rejects_amount_above_authority_limit(db_session, sod_world, api):
    claim = _make_claim(db_session, sod_world, created_by=sod_world.manager, amount=Decimal("1500.00"))
    resp = _approve(api, claim.id, _token(sod_world, sod_world.capped, "approver"))
    assert resp.status_code == 403
    assert "authority limit" in resp.json()["detail"].lower()
    db_session.refresh(claim)
    assert claim.status == "in_review"


def test_api_allows_amount_at_or_below_authority_limit(db_session, sod_world, api):
    at_limit = _make_claim(db_session, sod_world, created_by=sod_world.manager, amount=Decimal("1000.00"))
    resp = _approve(api, at_limit.id, _token(sod_world, sod_world.capped, "approver"))
    assert resp.status_code == 200, "amount == authority_limit must be allowed (not >)"

    below = _make_claim(db_session, sod_world, created_by=sod_world.manager, amount=Decimal("500.00"))
    resp = _approve(api, below.id, _token(sod_world, sod_world.capped, "approver"))
    assert resp.status_code == 200


# =========================================================================== #
# 4. Grant scope — service guard (precise both sides) + API (defense in depth)
# =========================================================================== #
def test_service_grant_guard_rejects_ungranted_allows_granted(db_session, sod_world):
    """The service guard, fed real resolved principals: an approver with no grant
    to the claim's client is rejected; one with the grant passes."""
    claim = _make_claim(db_session, sod_world, created_by=sod_world.manager)

    p_ungranted = build_principal(
        db_session, {"user_id": str(sod_world.ungranted), "firm_id": str(sod_world.firm)}
    )
    assert p_ungranted.allowed_client_ids == frozenset()  # no grant resolved
    with pytest.raises(SoDViolation, match="grant"):
        check_can_approve(claim, p_ungranted)

    p_granted = build_principal(
        db_session, {"user_id": str(sod_world.approver), "firm_id": str(sod_world.firm)}
    )
    assert sod_world.client in p_granted.allowed_client_ids
    check_can_approve(claim, p_granted)  # does not raise


def test_api_allows_granted_approver(db_session, sod_world, api):
    claim = _make_claim(db_session, sod_world, created_by=sod_world.manager)
    resp = _approve(api, claim.id, _token(sod_world, sod_world.approver, "approver"))
    assert resp.status_code == 200
    db_session.refresh(claim)
    assert claim.status == "approved" and claim.approved_by_user_id == sod_world.approver


def test_api_blocks_ungranted_approver(db_session, sod_world, api):
    """An ungranted approver cannot act on the client's claim. Defense in depth:
    the SoD grant guard returns 403; RLS (empty allowed_clients) would 404 — either
    way the claim is never approved."""
    claim = _make_claim(db_session, sod_world, created_by=sod_world.manager)
    resp = _approve(api, claim.id, _token(sod_world, sod_world.ungranted, "approver"))
    assert resp.status_code in (403, 404)
    db_session.refresh(claim)
    assert claim.status == "in_review" and claim.approved_by_user_id is None


# =========================================================================== #
# 5. Role scope — a Viewer cannot approve (§8), an Approver can (contrast)
# =========================================================================== #
def test_api_rejects_viewer_approval(db_session, sod_world, api):
    claim = _make_claim(db_session, sod_world, created_by=sod_world.manager)
    resp = _approve(api, claim.id, _token(sod_world, sod_world.viewer, "viewer"))
    assert resp.status_code == 403
    assert "viewer" in resp.json()["detail"].lower()
    db_session.refresh(claim)
    assert claim.status == "in_review"
