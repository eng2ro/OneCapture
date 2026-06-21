"""ERP Sync review workflow (FR-S5): queue actions, approve→release, SoD, RLS.

Against the real Postgres test DB (skips when none is reachable). Each test stages
the synthetic month into ``erpsync_entry`` (3 clean, 3 flagged, 1 held), then drives
the review service under a genuine :class:`Principal`. Covers:

* action correctness — approve / remap (recompute) / dismiss transitions + audit;
* approve→release — approved rows release through the SAME ``release_clean`` path
  as auto-clean rows; dismissed/flagged rows never reach the ledger;
* SoD — viewer / ungranted / over-authority-limit / maker-checker, at the service
  guard AND the ``ck_erpsync_entry_sod`` DB CHECK;
* RLS + app-layer scoping of the queue;
* the token→principal→route path end to end via the JSON API.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from eclaim.auth import tokens
from eclaim.auth.principal import Principal
from eclaim.config import get_settings
from eclaim.db.models import AppUser, ErpsyncEntry
from erpsync.persistence.pg_staging import PgStagingStore
from erpsync.persistence.store import Store
from erpsync.pipeline import run_import
from erpsync.release.service import release_clean
from erpsync.review import service
from erpsync.review.service import RemapInput, ReviewSoDViolation
from gen_synthetic import month_rows, write_csv


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _stage_month(db_session, config, tmp_path) -> dict:
    ids = db_session.info["principal"]
    sink = PgStagingStore(db_session, firm_id=ids["firm"], client_id=ids["client"])
    listing = tmp_path / "month.csv"
    write_csv(listing, month_rows())
    run_import(listing, config, Store(), staging=sink)
    db_session.flush()
    return ids


def _make_user(db_session, ids, role="manager") -> uuid.UUID:
    user = AppUser(
        firm_id=ids["firm"],
        email=f"{role}-{uuid.uuid4().hex[:8]}@review.test",
        display_name=f"Rev {role}",
        base_role=role,
    )
    db_session.add(user)
    db_session.flush()
    return user.id


def _principal(ids, user_id, *, role="manager", clients=None, authority=None) -> Principal:
    return Principal(
        user_id=user_id,
        firm_id=ids["firm"],
        base_role=role,
        allowed_client_ids=frozenset(clients if clients is not None else {ids["client"]}),
        authority_limit=authority,
        email=f"{role}@review.test",
    )


def _one(db_session, client_id, status) -> ErpsyncEntry:
    return db_session.execute(
        select(ErpsyncEntry)
        .where(ErpsyncEntry.client_id == client_id, ErpsyncEntry.status == status)
        .limit(1)
    ).scalar_one()


def _events(db_session, entry_id):
    from eclaim.repositories import AuditRepository

    return [e.event_type for e in AuditRepository(db_session).chain("erpsync_entry", entry_id)]


# --------------------------------------------------------------------------- #
# Action correctness
# --------------------------------------------------------------------------- #
def test_approve_held_and_flagged_transition_to_approved(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    reviewer = _principal(ids, _make_user(db_session, ids))

    held = _one(db_session, ids["client"], "held")
    flagged = _one(db_session, ids["client"], "flagged")

    a = service.approve(db_session, entry_id=held.id, reviewer=reviewer, note="distinct")
    b = service.approve(db_session, entry_id=flagged.id, reviewer=reviewer)

    assert a.status == "approved" and a.reviewed_by_user_id == reviewer.user_id
    assert b.status == "approved"
    assert _events(db_session, held.id) == ["approved"]


def test_remap_flagged_recomputes_tco2e_and_records_editor(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    editor = _principal(ids, _make_user(db_session, ids))
    flagged = _one(db_session, ids["client"], "flagged")

    remapped = service.remap(
        db_session,
        entry_id=flagged.id,
        mapping=RemapInput(
            category="Remapped fuel", scope="scope_1", basis="activity",
            factor_ref="TEST_FACTOR", factor_value=Decimal("2.5"),
            quantity=Decimal("10"), uom="L",
        ),
        reviewer=editor,
    )

    assert remapped.tco2e == Decimal("0.025000")  # 10 * 2.5 / 1000, 6dp
    assert remapped.data_quality == "measured"
    assert remapped.factor_ref == "TEST_FACTOR"
    assert remapped.edited_by_user_id == editor.user_id
    assert remapped.status == "flagged"  # remap edits; a separate approve releases
    assert _events(db_session, flagged.id) == ["remapped"]


def test_dismiss_reject_duplicate_and_dismiss_are_terminal(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    reviewer = _principal(ids, _make_user(db_session, ids))

    held = _one(db_session, ids["client"], "held")
    flagged = _one(db_session, ids["client"], "flagged")

    d1 = service.dismiss(
        db_session, entry_id=held.id, reviewer=reviewer,
        event_type="rejected_duplicate", note="already in e-Claim",
    )
    d2 = service.dismiss(db_session, entry_id=flagged.id, reviewer=reviewer, note="out of scope")

    assert d1.status == "dismissed" and d1.review_note == "already in e-Claim"
    assert d2.status == "dismissed"
    assert _events(db_session, held.id) == ["rejected_duplicate"]


# --------------------------------------------------------------------------- #
# Approve → release through the shared path
# --------------------------------------------------------------------------- #
def test_approved_rows_release_with_clean_rows_dismissed_excluded(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    reviewer = _principal(ids, _make_user(db_session, ids))

    flagged = db_session.execute(
        select(ErpsyncEntry).where(
            ErpsyncEntry.client_id == ids["client"], ErpsyncEntry.status == "flagged"
        )
    ).scalars().all()
    held = _one(db_session, ids["client"], "held")

    service.approve(db_session, entry_id=flagged[0].id, reviewer=reviewer)   # → approved
    service.approve(db_session, entry_id=held.id, reviewer=reviewer)          # → approved
    service.dismiss(db_session, entry_id=flagged[1].id, reviewer=reviewer)    # → dismissed
    # flagged[2] stays flagged

    batch = release_clean(db_session, firm_id=ids["firm"], client_id=ids["client"], actor="r")

    # 3 clean + 2 approved release; flagged + dismissed do not.
    assert batch.record_count == 5
    counts = dict(
        db_session.execute(
            text("SELECT status, count(*) FROM erpsync_entry WHERE client_id = :c GROUP BY status"),
            {"c": ids["client"]},
        ).all()
    )
    assert counts == {"released": 5, "flagged": 1, "dismissed": 1}


# --------------------------------------------------------------------------- #
# SoD — service guard
# --------------------------------------------------------------------------- #
def test_viewer_cannot_review(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    viewer = _principal(ids, uuid.uuid4(), role="viewer")
    flagged = _one(db_session, ids["client"], "flagged")
    with pytest.raises(ReviewSoDViolation, match="viewers"):
        service.approve(db_session, entry_id=flagged.id, reviewer=viewer)


def test_ungranted_reviewer_blocked(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    ungranted = _principal(ids, uuid.uuid4(), role="approver", clients=set())
    flagged = _one(db_session, ids["client"], "flagged")
    with pytest.raises(ReviewSoDViolation, match="no grant"):
        service.approve(db_session, entry_id=flagged.id, reviewer=ungranted)


def test_over_authority_limit_blocked(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    flagged = db_session.execute(
        select(ErpsyncEntry).where(
            ErpsyncEntry.client_id == ids["client"],
            ErpsyncEntry.status == "flagged",
            ErpsyncEntry.amount.is_not(None),
        ).limit(1)
    ).scalar_one()
    low = _principal(ids, uuid.uuid4(), role="approver", authority=flagged.amount - Decimal("1"))
    with pytest.raises(ReviewSoDViolation, match="authority limit"):
        service.approve(db_session, entry_id=flagged.id, reviewer=low)


def test_maker_checker_service_guard(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    user_a, user_b = _make_user(db_session, ids), _make_user(db_session, ids)
    flagged = _one(db_session, ids["client"], "flagged")

    remap_input = RemapInput(
        category="X", scope="scope_1", basis="activity",
        factor_ref="F", factor_value=Decimal("1"), quantity=Decimal("1"), uom="L",
    )
    service.remap(db_session, entry_id=flagged.id, mapping=remap_input,
                  reviewer=_principal(ids, user_a))

    # The maker (A) cannot approve their own remap...
    with pytest.raises(ReviewSoDViolation, match="cannot review"):
        service.approve(db_session, entry_id=flagged.id, reviewer=_principal(ids, user_a))

    # ...but a different reviewer (B) can.
    approved = service.approve(db_session, entry_id=flagged.id, reviewer=_principal(ids, user_b))
    assert approved.status == "approved" and approved.reviewed_by_user_id == user_b


# --------------------------------------------------------------------------- #
# SoD — DB CHECK (defence in depth)
# --------------------------------------------------------------------------- #
def test_db_check_blocks_same_maker_and_checker(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)
    user_a = _make_user(db_session, ids)
    flagged = _one(db_session, ids["client"], "flagged")
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            flagged.edited_by_user_id = user_a
            flagged.reviewed_by_user_id = user_a  # violates ck_erpsync_entry_sod
            db_session.flush()


# --------------------------------------------------------------------------- #
# RLS + app-layer queue scoping
# --------------------------------------------------------------------------- #
def test_queue_scoping_and_rls(db_session, config, tmp_path):
    ids = _stage_month(db_session, config, tmp_path)

    # App-layer narrowing: granted → the 4 held/flagged rows; ungranted → none.
    assert len(service.review_queue(db_session, frozenset({ids["client"]}))) == 4
    assert service.review_queue(db_session, frozenset()) == []
    assert service.review_queue(db_session, frozenset({uuid.uuid4()})) == []

    # RLS: under a foreign firm context, the rows are invisible even when asked for.
    db_session.execute(
        text("SELECT set_config('app.current_firm', :v, true)"), {"v": str(uuid.uuid4())}
    )
    assert service.review_queue(db_session, frozenset({ids["client"]})) == []
    db_session.execute(
        text("SELECT set_config('app.current_firm', :v, true)"), {"v": str(ids["firm"])}
    )


# --------------------------------------------------------------------------- #
# API — token → principal → route
# --------------------------------------------------------------------------- #
def test_api_queue_and_approve_under_real_token(db_session, config, tmp_path):
    from fastapi.testclient import TestClient

    from eclaim.api import deps
    from eclaim.api.app import create_app

    ids = _stage_month(db_session, config, tmp_path)

    def _override_db():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    token = tokens.mint(
        {"user_id": str(ids["user"]), "firm_id": str(ids["firm"]), "base_role": "partner"},
        secret=get_settings().jwt_secret,
        ttl_seconds=300,
    )
    auth = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as c:
        q = c.get("/api/erpsync/queue", headers=auth)
        assert q.status_code == 200
        rows = q.json()
        assert len(rows) == 4  # 3 flagged + 1 held
        assert {r["status"] for r in rows} == {"flagged", "held"}

        target = rows[0]["id"]
        r = c.post(f"/api/erpsync/entries/{target}/approve", json={}, headers=auth)
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    app.dependency_overrides.clear()
