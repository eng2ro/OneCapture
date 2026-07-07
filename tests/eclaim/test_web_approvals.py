"""Approval matrix admin UI (Appendix B, Part 2) — the Phase-1 template picker +
single-tier editor under /admin, and that what it writes actually governs approval.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from eclaim.auth.principal import Principal
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimService, Repos
from eclaim.services.sod import SoDViolation


def _cid(db_session):
    return db_session.info["principal"]["client"]


def test_admin_approvals_page_renders(client):
    page = client.get("/admin/approvals")
    assert page.status_code == 200
    assert "Approval matrix" in page.text
    assert "Small business" in page.text and "Setup wizard" in page.text


def test_apply_template_writes_rules(client, db_session):
    cid = _cid(db_session)
    resp = client.post(
        "/admin/approvals/template",
        data={"client_id": str(cid), "template": "small"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    rules = Repos.for_session(db_session).approvals.rules_for_client(cid)
    assert len(rules) == 2
    assert {r.approver_role for r in rules} == {"manager", "partner"}


def test_reapplying_a_template_replaces_not_appends(client, db_session):
    cid = _cid(db_session)
    for tpl in ("growing", "starter"):     # growing = 3 rows, starter = 1
        client.post("/admin/approvals/template",
                    data={"client_id": str(cid), "template": tpl}, follow_redirects=False)
    rules = Repos.for_session(db_session).approvals.rules_for_client(cid)
    assert len(rules) == 1                 # replaced, not accumulated


def test_add_then_delete_band(client, db_session):
    cid = _cid(db_session)
    client.post("/admin/approvals/add", data={
        "client_id": str(cid), "min_amount": "1000", "max_amount": "",
        "approver_role": "partner", "approvals_required": "1",
    }, follow_redirects=False)
    repos = Repos.for_session(db_session)
    rules = repos.approvals.rules_for_client(cid)
    assert len(rules) == 1 and rules[0].approver_role == "partner"

    resp = client.post("/admin/approvals/delete",
                       data={"rule_id": str(rules[0].id), "client_id": str(cid)},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert repos.approvals.rules_for_client(cid) == []


# --------------------------------------------------------------------------- #
# F7 — the launch matrix is scoped to e-Claim, not silently governing AP
# --------------------------------------------------------------------------- #
def test_admin_rules_are_scoped_to_eclaim(client, db_session):
    """F7: the launch admin UI configures the e-Claim matrix, so the rows it writes
    must be scope_module='eclaim' — not NULL, which would silently govern AP too."""
    cid = _cid(db_session)
    client.post("/admin/approvals/template",
                data={"client_id": str(cid), "template": "small"}, follow_redirects=False)
    client.post("/admin/approvals/add", data={
        "client_id": str(cid), "min_amount": "5000", "max_amount": "",
        "approver_role": "partner",
    }, follow_redirects=False)
    rules = Repos.for_session(db_session).approvals.rules_for_client(cid)
    assert rules and all(r.scope_module == "eclaim" for r in rules)


def test_eclaim_admin_matrix_does_not_govern_ap(client, db_session):
    """An e-Claim matrix band must NOT bind an AP invoice (F7) — AP is governed only by
    an ap-scoped matrix (or its SoD + authority fallback)."""
    from decimal import Decimal as _D

    from eclaim.db.models import ApInvoice, Vendor
    from eclaim.services import ap

    cid = _cid(db_session)
    ids = db_session.info["principal"]
    client.post("/admin/approvals/template",
                data={"client_id": str(cid), "template": "growing"}, follow_redirects=False)

    v = Vendor(firm_id=ids["firm"], client_id=cid, name="V")
    db_session.add(v)
    db_session.flush()
    inv = ApInvoice(firm_id=ids["firm"], client_id=cid, vendor_id=v.id,
                    total_amount=_D("50000"), idempotency_key="k7")
    db_session.add(inv)
    db_session.flush()
    assert ap.matrix_rule_for_invoice(db_session, inv) is None   # e-Claim rules don't bind AP


def _backfill_sql():
    import importlib.util
    from pathlib import Path

    path = (Path(__file__).resolve().parents[2] / "src" / "eclaim" / "alembic"
            / "versions" / "0029_matrix_scope_backfill.py")
    spec = importlib.util.spec_from_file_location("_mig_0029", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BACKFILL


def test_migration_0029_backfills_null_scope_to_eclaim(client, db_session):
    """Pins the migration's UPDATE: a legacy NULL-scope rule is clamped to 'eclaim'."""
    from sqlalchemy import text

    from eclaim.db.models import ApprovalMatrixRule
    ids = db_session.info["principal"]
    legacy = ApprovalMatrixRule(
        firm_id=ids["firm"], client_id=ids["client"], step_order=1,
        approver_role="partner", approvals_required=1, active=True, scope_module=None,
    )
    db_session.add(legacy)
    db_session.flush()
    db_session.execute(text(_backfill_sql()))
    db_session.expire_all()
    assert db_session.get(ApprovalMatrixRule, legacy.id).scope_module == "eclaim"


@pytest.mark.parametrize("template", ["starter", "small", "growing", "enterprise"])
def test_launch_templates_seed_single_approval(client, db_session, template):
    """P1: no launch template may seed an ``approvals_required > 1`` band — the engine
    enforces a single sign-off in Phase-1, so a >1 row would be a fake control. Fails
    if any template row is reverted to a multi-approval count."""
    cid = _cid(db_session)
    client.post("/admin/approvals/template",
                data={"client_id": str(cid), "template": template}, follow_redirects=False)
    rules = Repos.for_session(db_session).approvals.rules_for_client(cid)
    assert rules, "template seeded no rules"
    assert all(r.approvals_required == 1 for r in rules), \
        f"{template} seeds an unenforced multi-approval band: " \
        f"{[r.approvals_required for r in rules]}"


def test_add_band_ignores_supplied_count(client, db_session):
    """P1: the add-band route must clamp ``approvals_required`` to 1 regardless of what
    the caller posts — a crafted POST must not persist an unenforced >1 control. Fails
    if the route ever reads the count from the form again."""
    cid = _cid(db_session)
    client.post("/admin/approvals/add", data={
        "client_id": str(cid), "min_amount": "10000", "max_amount": "",
        "approver_role": "partner", "approvals_required": "5",
    }, follow_redirects=False)
    rules = Repos.for_session(db_session).approvals.rules_for_client(cid)
    assert len(rules) == 1
    assert rules[0].approvals_required == 1, \
        "crafted POST persisted an unenforced multi-approval control"


def test_applied_template_governs_approval(client, fake_ocr, db_session, tmp_path):
    cid = _cid(db_session)
    ids = db_session.info["principal"]
    client.post("/admin/approvals/template",
                data={"client_id": str(cid), "template": "small"}, follow_redirects=False)

    svc, repos = ClaimService(), Repos.for_session(db_session)
    claim = svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])
    fake_ocr.extraction = Extraction(expense_type="other", total_amount=Decimal("5000"))
    svc.add_line(repos=repos, claim=claim, image_bytes=b"\x89PNG img",
                 media_type="image/png", ocr=fake_ocr, image_dir=tmp_path)

    def _p(role):
        return Principal(user_id=ids["user"], firm_id=ids["firm"], base_role=role,
                         allowed_client_ids=frozenset({ids["client"]}), email=f"{role}@seed.test")

    with pytest.raises(SoDViolation):        # >2,000 band needs a partner
        svc.approve(repos=repos, claim_id=claim.id, actor="m", approver=_p("manager"))
    svc.approve(repos=repos, claim_id=claim.id, actor="p", approver=_p("partner"))
    assert db_session.get(type(claim), claim.id).status == "approved"
