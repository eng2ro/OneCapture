"""Viewers are read-only on the WEB capture surface too (gap found in the
role-gating audit, 2026-07-08): POST /capture and POST /capture/mileage used to
skip the viewer check every other mutating web route has — the bearer API's
upload endpoint always blocked viewers, so the web form was the one open door.
"""

from __future__ import annotations

import uuid

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from eclaim.db.models import Claim


def _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, base_role: str):
    """A TestClient whose principal is forced to ``base_role`` — mirrors the
    ``client`` fixture's overrides (same pattern as test_web_auth)."""
    from eclaim.api import deps
    from eclaim.api.app import create_app
    from eclaim.auth.principal import Principal

    def _override_db():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    def _principal(request: Request) -> Principal:
        ids = db_session.info["principal"]
        principal = Principal(
            user_id=ids["user"], firm_id=ids["firm"], base_role=base_role,
            allowed_client_ids=frozenset({ids["client"]}), email=f"{base_role}@seed.test",
        )
        request.state.principal = principal
        request.state.db = db_session
        return principal

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    app.dependency_overrides[deps.get_principal] = _principal
    app.dependency_overrides[deps.get_session_principal] = _principal
    app.dependency_overrides[deps.get_ocr] = lambda: fake_ocr
    app.dependency_overrides[deps.get_segmenter] = lambda: fake_segmenter
    app.dependency_overrides[deps.get_image_dir] = lambda: tmp_path
    return app


def _claim_count(db_session) -> int:
    return db_session.execute(select(func.count()).select_from(Claim)).scalar_one()


_CAPTURE_POST = {
    "attested": "yes",
    "items": '[{"expense_type": "other", "total_amount": "10"}]',
}
_FILES = [("files", ("r.png", b"\x89PNG\r\n fake", "image/png"))]


def test_viewer_blocked_from_every_web_capture_route(db_session, fake_ocr, fake_segmenter, tmp_path):
    app = _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, "viewer")
    with TestClient(app) as c:
        before = _claim_count(db_session)

        # The form page itself: friendly in-shell 403, not the capture UI.
        page = c.get("/capture")
        assert page.status_code == 403
        assert "view-only" in page.text

        # Claim creation — both the receipt batch and the mileage path.
        assert c.post("/capture", files=_FILES, data=_CAPTURE_POST,
                      follow_redirects=False).status_code == 403
        assert c.post("/capture/mileage", data={
            "origin": "KL", "destination": "Seremban", "trip_date": "2026-07-05",
            "attested": "yes", "vehicle_id": "",
        }, follow_redirects=False).status_code == 403

        # The OCR pre-read endpoint (no persistence, but writer-only work).
        assert c.post("/capture/extract", files={
            "file": ("r.png", b"\x89PNG\r\n fake", "image/png")
        }).status_code == 403

        assert _claim_count(db_session) == before        # nothing was created


def test_viewer_sidebar_has_no_new_claim_button(db_session, fake_ocr, fake_segmenter, tmp_path):
    app = _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, "viewer")
    with TestClient(app) as c:
        assert "New claim" not in c.get("/claims").text


def test_approver_still_captures_normally(db_session, fake_ocr, fake_segmenter, tmp_path):
    """The guard hits ONLY viewers — a client-scoped approver keeps working."""
    app = _app_as(db_session, fake_ocr, fake_segmenter, tmp_path, "approver")
    with TestClient(app) as c:
        assert "New claim" in c.get("/claims").text
        resp = c.post("/capture", files=_FILES, data=_CAPTURE_POST, follow_redirects=False)
        assert resp.status_code == 303
        cid = resp.headers["location"].split("/")[2]
        assert db_session.get(Claim, uuid.UUID(cid)) is not None
