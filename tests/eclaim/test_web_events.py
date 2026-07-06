"""Events + budgets (Phase 4): create an event, attach claims to it, and see the
budget rollup + related-claims consolidation on review.

An event groups claims across people and holds a budget. Capture can attach a
claim to an event; the review screen then shows the event panel, a budget bar
(spent vs budget across ALL the event's claims), and a related-claims strip — the
late-bill / split-claim view.
"""

from __future__ import annotations

import json
import re

from sqlalchemy import select

from eclaim.db.models import Claim, Event


def _files(n=1):
    return [("files", (f"r{i}.png", b"\x89PNG\r\n fake", "image/png")) for i in range(n)]


def _capture(client, items, *, title="", event_id=""):
    return client.post(
        "/capture",
        files=_files(len(items)),
        data={"items": json.dumps(items), "title": title, "event_id": event_id, "attested": "yes"},
        follow_redirects=False,
    )


def _claim_id(resp) -> str:
    return re.match(r"^/claims/([0-9a-f-]+)/review$", resp.headers["location"]).group(1)


def test_admin_create_event_and_capture_attaches_it(client, db_session):
    ids = db_session.info["principal"]
    # Create an event with a budget via the admin page.
    r = client.post("/admin/events", data={
        "client_id": str(ids["client"]), "title": "A City — Sales Training",
        "purpose": "Regional enablement", "event_type": "training",
        "attendee_count": "20", "budget_amount": "10000", "budget_currency": "MYR",
    }, follow_redirects=False)
    assert r.status_code == 303
    event = db_session.execute(select(Event)).scalars().one()
    assert event.title == "A City — Sales Training" and event.budget_amount == 10000

    # The capture page offers the event; capturing with it attaches the claim.
    page = client.get("/capture").text
    assert "A City — Sales Training" in page

    resp = _capture(
        client,
        items=[{"expense_type": "other", "total_amount": "3000", "vendor": "Hilton"}],
        title="Venue", event_id=str(event.id),
    )
    cid = _claim_id(resp)
    claim = db_session.get(Claim, __import__("uuid").UUID(cid))
    assert claim.event_id == event.id


def test_review_shows_budget_and_related_claims(client, db_session):
    ids = db_session.info["principal"]
    client.post("/admin/events", data={
        "client_id": str(ids["client"]), "title": "KL Trip", "budget_amount": "5000",
    })
    event = db_session.execute(select(Event)).scalars().one()

    # Two separate claims on the same event (e.g. two people / a late bill).
    _capture(client, items=[{"expense_type": "other", "total_amount": "3000", "vendor": "Hotel"}],
             event_id=str(event.id))
    resp2 = _capture(client, items=[{"expense_type": "other", "total_amount": "1200", "vendor": "Flights"}],
                     event_id=str(event.id))
    cid2 = _claim_id(resp2)

    page = client.get(f"/claims/{cid2}/review").text
    # Budget bar: combined spend across BOTH claims (3000 + 1200) vs the 5000 budget.
    assert "KL Trip" in page
    assert "4200.00" in page and "5000.00" in page
    # Related-claims strip surfaces the other claim on the event.
    assert "claims for this event" in page
