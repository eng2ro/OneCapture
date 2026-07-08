"""Per-client settings registry (owner request 2026-07-08): behaviour controls
are configurable per company — e.g. allow auto-reverse or not. Configuration,
never a per-customer code branch; integrity rules are not settable.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from eclaim.db.models import ClaimLine
from eclaim.ocr.base import Extraction
from eclaim.services import settings as settings_service
from eclaim.services.claims import ClaimError, ClaimService, IllegalTransition, Repos
from eclaim.services.sod import SoDViolation


def _set(db_session, key, value):
    ids = db_session.info["principal"]
    settings_service.set_setting(
        db_session, firm_id=ids["firm"], client_id=ids["client"],
        key=key, value=value, actor="t",
    )


# --------------------------------------------------------------------------- #
# Registry mechanics
# --------------------------------------------------------------------------- #
def test_default_applies_until_set_and_junk_falls_back(client, db_session):
    ids = db_session.info["principal"]
    assert settings_service.get(db_session, ids["client"], "carbon.auto_reverse") == "allow"

    _set(db_session, "carbon.auto_reverse", "off")
    assert settings_service.get(db_session, ids["client"], "carbon.auto_reverse") == "off"

    # A stored value the current registry doesn't define falls back to the
    # default — an old value never grants behaviour the code doesn't define.
    row = db_session.execute(
        select(settings_service.ClientSetting).where(
            settings_service.ClientSetting.client_id == ids["client"],
            settings_service.ClientSetting.key == "carbon.auto_reverse",
        )
    ).scalars().one()
    row.value = "yolo"
    db_session.flush()
    assert settings_service.get(db_session, ids["client"], "carbon.auto_reverse") == "allow"


def test_set_validates_and_audits_old_to_new(client, db_session):
    ids = db_session.info["principal"]
    with pytest.raises(ValueError):
        settings_service.set_setting(
            db_session, firm_id=ids["firm"], client_id=ids["client"],
            key="carbon.auto_reverse", value="nonsense", actor="t",
        )
    with pytest.raises(ValueError):
        settings_service.set_setting(
            db_session, firm_id=ids["firm"], client_id=ids["client"],
            key="not.a.setting", value="on", actor="t",
        )
    row = settings_service.set_setting(
        db_session, firm_id=ids["firm"], client_id=ids["client"],
        key="carbon.auto_reverse", value="approver_reason", actor="t",
    )
    chain = Repos.for_session(db_session).audit.chain("client_setting", row.id)
    changed = [e for e in chain if e.event_type == "setting_changed"]
    assert changed and changed[-1].detail["from"] == "allow"
    assert changed[-1].detail["to"] == "approver_reason"


# --------------------------------------------------------------------------- #
# carbon.auto_reverse governs reverse()
# --------------------------------------------------------------------------- #
def _released_claim(client, fake_ocr):
    fake_ocr.extraction = Extraction(
        vendor="Shell", expense_type="fuel_diesel",
        quantity=Decimal("10"), unit="L", total_amount=Decimal("30.00"),
    )
    files = {"file": ("r.png", b"\x89PNG rev-setting", "image/png")}
    cid = client.post("/api/claims/upload", files=files,
                      data={"attested": "true"}).json()["id"]
    assert client.post(f"/api/claims/{cid}/approve").status_code == 200
    assert client.post(f"/api/claims/{cid}/release").status_code == 200
    return cid


def test_auto_reverse_off_blocks_reversal(client, fake_ocr, db_session):
    cid = _released_claim(client, fake_ocr)
    _set(db_session, "carbon.auto_reverse", "off")
    db_session.commit()
    r = client.post(f"/api/claims/{cid}/reverse")
    assert r.status_code in (400, 409)
    assert "disabled" in r.json()["detail"]


def test_auto_reverse_approver_reason_requires_both(client, fake_ocr, db_session):
    cid = _released_claim(client, fake_ocr)
    _set(db_session, "carbon.auto_reverse", "approver_reason")
    db_session.commit()

    # The test principal is a partner (senior enough) — but no reason → refused.
    r = client.post(f"/api/claims/{cid}/reverse")
    assert r.status_code in (400, 409)
    assert "reason" in r.json()["detail"]

    # With a reason it reverses, and the reason rides the audit event.
    r2 = client.post(f"/api/claims/{cid}/reverse?reason=duplicate capture")
    assert r2.status_code == 200
    events = client.get(f"/api/audit/{cid}").json()
    reversed_ev = next(e for e in events if e["event_type"] == "reversed")
    assert reversed_ev["detail"]["reason"] == "duplicate capture"


def test_auto_reverse_approver_reason_blocks_junior_roles(client, fake_ocr, db_session):
    from eclaim.auth.principal import Principal

    ids = db_session.info["principal"]
    cid = _released_claim(client, fake_ocr)
    _set(db_session, "carbon.auto_reverse", "approver_reason")
    approver = Principal(user_id=ids["user"], firm_id=ids["firm"], base_role="approver",
                         allowed_client_ids=frozenset({ids["client"]}), email="jr@seed.test")
    svc, repos = ClaimService(), Repos.for_session(db_session)
    with pytest.raises(SoDViolation, match="manager or partner"):
        svc.reverse(repos=repos, claim_id=uuid.UUID(cid), actor="jr",
                    principal=approver, reason="still not allowed")


def test_auto_reverse_allow_keeps_current_behaviour(client, fake_ocr, db_session):
    cid = _released_claim(client, fake_ocr)          # default 'allow'
    assert client.post(f"/api/claims/{cid}/reverse").status_code == 200


# --------------------------------------------------------------------------- #
# fx.auto_prefill governs the FX default
# --------------------------------------------------------------------------- #
def test_fx_auto_prefill_off_leaves_rate_for_a_human(client, fake_ocr, db_session):
    import datetime as dt

    from eclaim.services import fx

    ids = db_session.info["principal"]
    fx.upsert_rate(db_session, firm_id=ids["firm"], client_id=ids["client"],
                   currency="USD", period=dt.date(2025, 9, 1),
                   rate_to_myr=Decimal("4.70"), actor="t")
    _set(db_session, "fx.auto_prefill", "off")
    db_session.commit()

    fake_ocr.extraction = Extraction(
        vendor="US Shop", total_amount=Decimal("100.00"), currency="USD",
        date="26 SEP 2025", expense_type="other",
    )
    files = {"file": ("r.png", b"\x89PNG fx-off", "image/png")}
    cid = client.post("/api/claims/upload", files=files,
                      data={"attested": "true"}).json()["id"]
    line = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == uuid.UUID(cid))
    ).scalars().one()
    assert line.fx_rate is None                      # table rate NOT auto-applied
    assert line.base_amount is None                  # honest: no MYR value yet


# --------------------------------------------------------------------------- #
# Admin page
# --------------------------------------------------------------------------- #
def test_admin_settings_page_renders_and_saves(client, db_session):
    ids = db_session.info["principal"]
    page = client.get("/admin/settings")
    assert page.status_code == 200
    assert "carbon.auto_reverse" in page.text and "fx.auto_prefill" in page.text

    r = client.post("/admin/settings", data={
        "client_id": str(ids["client"]), "key": "carbon.auto_reverse",
        "value": "approver_reason",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert settings_service.get(
        db_session, ids["client"], "carbon.auto_reverse"
    ) == "approver_reason"

    bad = client.post("/admin/settings", data={
        "client_id": str(ids["client"]), "key": "carbon.auto_reverse",
        "value": "nonsense",
    })
    assert "not one of" in bad.text
