"""Approval authority matrix engine (Appendix B, Part 1).

The matrix decides who may approve a claim by amount band. The launch engine reads
step_order=1 only; a higher role satisfies a lower requirement; a client with no
matrix falls back to legacy behaviour (any authorised approver within their
personal authority_limit).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from eclaim.auth.principal import Principal
from eclaim.db.models import ApprovalMatrixRule
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimService, Repos
from eclaim.services.sod import SoDViolation, check_can_approve, matrix_rule_for


def _p(ids, role, *, user_id=None):
    return Principal(
        user_id=user_id or ids["user"], firm_id=ids["firm"], base_role=role,
        allowed_client_ids=frozenset({ids["client"]}), email=f"{role}@seed.test",
    )


def _claim_of(svc, repos, fake_ocr, tmp_path, ids, amount):
    claim = svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])
    fake_ocr.extraction = Extraction(expense_type="other", total_amount=Decimal(amount))
    svc.add_line(
        repos=repos, claim=claim, image_bytes=b"\x89PNG img", media_type="image/png",
        ocr=fake_ocr, image_dir=tmp_path,
    )
    return claim


def _rule(db_session, ids, **kw):
    base = dict(
        firm_id=ids["firm"], client_id=ids["client"], step_order=1,
        approvals_required=1, active=True,
    )
    base.update(kw)
    rule = ApprovalMatrixRule(**base)
    db_session.add(rule)
    db_session.flush()
    return rule


def test_no_matrix_falls_back_to_legacy(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    claim = _claim_of(svc, repos, fake_ocr, tmp_path, ids, "1500")
    assert matrix_rule_for(repos, claim) is None
    check_can_approve(claim, _p(ids, "manager"))          # no rule → any approver ok


def test_partner_band_blocks_manager_allows_partner(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    _rule(db_session, ids, min_amount=Decimal("1000"), max_amount=None, approver_role="partner")
    claim = _claim_of(svc, repos, fake_ocr, tmp_path, ids, "1500")

    rule = matrix_rule_for(repos, claim)
    assert rule is not None
    with pytest.raises(SoDViolation):
        check_can_approve(claim, _p(ids, "manager"), matrix_rule=rule)   # too junior
    check_can_approve(claim, _p(ids, "partner"), matrix_rule=rule)       # senior enough


def test_role_adequacy_manager_rule_allows_partner_blocks_approver(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    _rule(db_session, ids, min_amount=None, max_amount=None, approver_role="manager")
    claim = _claim_of(svc, repos, fake_ocr, tmp_path, ids, "50")
    rule = matrix_rule_for(repos, claim)
    check_can_approve(claim, _p(ids, "partner"), matrix_rule=rule)       # partner >= manager
    with pytest.raises(SoDViolation):
        check_can_approve(claim, _p(ids, "approver"), matrix_rule=rule)  # approver < manager


def test_amount_below_band_is_ungoverned(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    _rule(db_session, ids, min_amount=Decimal("1000"), max_amount=None, approver_role="partner")
    claim = _claim_of(svc, repos, fake_ocr, tmp_path, ids, "200")        # under the band floor
    assert matrix_rule_for(repos, claim) is None
    check_can_approve(claim, _p(ids, "manager"))                         # legacy → ok


def test_named_user_rule(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    _rule(db_session, ids, min_amount=None, max_amount=None, approver_user_id=ids["user"])
    claim = _claim_of(svc, repos, fake_ocr, tmp_path, ids, "50")
    rule = matrix_rule_for(repos, claim)
    check_can_approve(claim, _p(ids, "partner", user_id=ids["user"]), matrix_rule=rule)
    with pytest.raises(SoDViolation):
        check_can_approve(claim, _p(ids, "partner", user_id=uuid.uuid4()), matrix_rule=rule)


def test_service_approve_enforces_matrix(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    _rule(db_session, ids, min_amount=Decimal("1000"), max_amount=None, approver_role="partner")
    claim = _claim_of(svc, repos, fake_ocr, tmp_path, ids, "5000")

    with pytest.raises(SoDViolation):
        svc.approve(repos=repos, claim_id=claim.id, actor="m", approver=_p(ids, "manager"))
    svc.approve(repos=repos, claim_id=claim.id, actor="p", approver=_p(ids, "partner"))
    assert db_session.get(type(claim), claim.id).status == "approved"
