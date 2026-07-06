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
