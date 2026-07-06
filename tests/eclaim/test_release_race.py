"""Release concurrency race (HIGH): two simultaneous /release (or /reverse) on one
claim must not double-release.

Serialisation is by a ``SELECT … FOR UPDATE`` lock on the claim, backed by a hard
``UNIQUE(client_id, batch_hash)`` on release_batch so a double-release cannot
persist even if the lock were bypassed; the service maps that collision to an
idempotent no-op. Real cross-connection blocking needs threads (flaky), so we test
the two guarantees deterministically: the DB constraint fires, and the service's
IntegrityError→recovery path returns the winner's batch instead of erroring.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.release import canonical_hash
from eclaim.auth.principal import Principal
from eclaim.db.models import Category, ReleaseBatch
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimService, Repos


def _partner(db_session) -> Principal:
    ids = db_session.info["principal"]
    return Principal(
        user_id=ids["user"], firm_id=ids["firm"], base_role="partner",
        allowed_client_ids=frozenset({ids["client"]}), email="partner@seed.test",
    )


def _batch(ids, digest: str, created_by: str = "t") -> ReleaseBatch:
    return ReleaseBatch(
        firm_id=ids["firm"], client_id=ids["client"], source_type="eclaim",
        created_by=created_by, batch_hash=digest, record_count=0, status="released",
    )


def test_release_batch_unique_constraint_rejects_duplicate(db_session):
    ids = db_session.info["principal"]
    db_session.add(_batch(ids, "DEADBEEF"))
    db_session.flush()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():        # isolates the failed flush
            db_session.add(_batch(ids, "DEADBEEF"))
            db_session.flush()


def test_release_collision_resolves_to_idempotent_no_op(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]

    noncarbon = Category(
        firm_id=ids["firm"], client_id=ids["client"], name="Stationery",
        expense_type="other", carbon_relevant=False,
    )
    db_session.add(noncarbon)
    db_session.flush()

    claim = svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])
    fake_ocr.extraction = Extraction(expense_type="other", total_amount=Decimal("40"))
    svc.add_line(
        repos=repos, claim=claim, image_bytes=b"\x89PNG\r\n fake", media_type="image/png",
        ocr=fake_ocr, image_dir=tmp_path, category_id=noncarbon.id,
    )
    # Attest so the out-of-pocket claim clears the release gate (P3).
    claim.attested_by = "claimant@seed.test"
    db_session.flush()
    svc.approve(repos=repos, claim_id=claim.id, actor="reviewer", approver=_partner(db_session))

    # A concurrent winner: the batch this release will compute already exists, while
    # the claim is still 'approved' so the status-based early-return does NOT fire —
    # forcing the insert to collide on UNIQUE(client_id, batch_hash).
    digest = canonical_hash([{"claim_id": str(claim.id)}])
    winner = _batch(ids, digest, created_by="winner")
    db_session.add(winner)
    db_session.flush()
    winner_id = winner.id

    result = svc.release(
        repos=repos, claim_id=claim.id, actor="loser", principal=_partner(db_session)
    )
    assert result.id == winner_id            # idempotent — returns the winner's batch

    # And no second batch was written for this content — the double-release is gone.
    n = db_session.execute(
        select(func.count()).select_from(ReleaseBatch).where(
            ReleaseBatch.client_id == ids["client"],
            ReleaseBatch.batch_hash == digest,
        )
    ).scalar_one()
    assert n == 1


def test_reverse_collision_resolves_to_idempotent_no_op(db_session, fake_ocr, tmp_path):
    """The reverse path has the SAME idempotent-recovery guarantee as release: two
    concurrent /reverse on one released claim collide on UNIQUE(client_id, batch_hash),
    and the loser returns the winner's reversal batch instead of a 500 (punch-list P7).
    """
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    noncarbon = Category(
        firm_id=ids["firm"], client_id=ids["client"], name="Stationery",
        expense_type="other", carbon_relevant=False,
    )
    db_session.add(noncarbon)
    db_session.flush()

    claim = svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])
    fake_ocr.extraction = Extraction(expense_type="other", total_amount=Decimal("40"))
    svc.add_line(
        repos=repos, claim=claim, image_bytes=b"\x89PNG\r\n fake", media_type="image/png",
        ocr=fake_ocr, image_dir=tmp_path, category_id=noncarbon.id,
    )
    claim.attested_by = "claimant@seed.test"   # clear the P3 release gate
    db_session.flush()
    svc.approve(repos=repos, claim_id=claim.id, actor="r", approver=_partner(db_session))
    svc.release(repos=repos, claim_id=claim.id, actor="r", principal=_partner(db_session))

    # A concurrent winner already anchored the reversal batch for this claim (the
    # zero-relevant reversal keys its hash off the claim id).
    digest = canonical_hash([{"reversal_of": str(claim.id)}])
    winner = _batch(ids, digest, created_by="winner")
    db_session.add(winner)
    db_session.flush()
    winner_id = winner.id

    result = svc.reverse(
        repos=repos, claim_id=claim.id, actor="loser", principal=_partner(db_session)
    )
    assert result.id == winner_id            # idempotent — returns the winner's batch

    n = db_session.execute(
        select(func.count()).select_from(ReleaseBatch).where(
            ReleaseBatch.client_id == ids["client"],
            ReleaseBatch.batch_hash == digest,
        )
    ).scalar_one()
    assert n == 1                            # no second reversal batch written


def test_locking_read_returns_claim(db_session):
    """The FOR UPDATE read used by the transitions resolves the row like a plain get
    (the lock only matters under concurrency); a missing id returns None."""
    ids = db_session.info["principal"]
    svc, repos = ClaimService(), Repos.for_session(db_session)
    claim = svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])
    assert repos.claims.lock_for_update(claim.id).id == claim.id
    assert repos.claims.lock_for_update(uuid.uuid4()) is None
