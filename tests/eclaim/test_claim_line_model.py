"""Phase 0 (migration 0008) — the new event + claim_line tables work end to end.

These run on the unprivileged ``onecapture_app`` session (``db_session``), so they
prove the new tables' RLS policies + grants are correct and the SQLAlchemy models
stay in lockstep with the migration (insert + read-back round-trips under the same
role the app uses). The claim/header split itself is exercised by the rest of the
suite, which still runs on the legacy ``claim`` columns Phase 0 deliberately keeps.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from eclaim.db.models import Claim, ClaimLine, Event


def _ids(db_session) -> dict:
    return db_session.info["principal"]


def test_event_round_trips_under_app_role(db_session):
    ids = _ids(db_session)
    event = Event(
        firm_id=ids["firm"],
        client_id=ids["client"],
        title="A City — Sales Training",
        purpose="Regional sales enablement",
        event_type="training",
        attendee_count=20,
        budget_amount=Decimal("10000.00"),
        budget_currency="MYR",
    )
    db_session.add(event)
    db_session.flush()

    got = db_session.execute(select(Event).where(Event.id == event.id)).scalar_one()
    assert got.title == "A City — Sales Training"
    assert got.attendee_count == 20
    assert got.budget_amount == Decimal("10000.00")
    assert got.status == "active"  # server default


def test_claim_line_defaults_and_link(db_session):
    ids = _ids(db_session)
    claim = Claim(
        firm_id=ids["firm"],
        client_id=ids["client"],
        image_path="/x/header.png",
        image_sha256="deadbeef",
        title="KL client trip",
        total_claimed=Decimal("1275.00"),
    )
    db_session.add(claim)
    db_session.flush()

    line = ClaimLine(
        firm_id=ids["firm"],
        client_id=ids["client"],
        claim_id=claim.id,
        line_no=1,
        vendor="Grab",
        total_amount=Decimal("60.00"),
        image_path="/x/grab.png",
        image_sha256="cafef00d",
    )
    db_session.add(line)
    db_session.flush()

    got = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == claim.id)
    ).scalar_one()
    # Server-side defaults from migration 0008.
    assert got.payment_method == "out_of_pocket"
    assert got.reimbursable is True
    assert got.carbon_class == "none"
    assert got.line_status == "pending"
    assert got.line_no == 1
    assert got.vendor == "Grab"
