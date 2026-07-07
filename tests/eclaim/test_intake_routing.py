"""Document classifier + router (C1).

The vision OCR now classifies each page (expense_receipt / vendor_invoice /
delivery_order / unknown); the router sends a staff expense into e-Claim and diverts
a vendor bill to the intake holding queue instead of silently forcing it into a
claim. These tests pin the pure routing rules, the tolerant provider coercion, and
the end-to-end capture → divert → holding-queue → correct flow.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from eclaim.db.models import Claim, DocumentIntake
from eclaim.ocr.anthropic_provider import _coerce_classification
from eclaim.ocr.base import Extraction
from eclaim.services import intake as intake_service
from eclaim.services import routing
from eclaim.services.claims import Repos


# --------------------------------------------------------------------------- #
# Pure router
# --------------------------------------------------------------------------- #
def test_expense_receipt_routes_to_eclaim():
    r = routing.route("expense_receipt", Decimal("0.95"))
    assert r.queue == routing.QUEUE_ECLAIM and not r.needs_manual


def test_missing_confidence_is_treated_as_confident_receipt():
    """A provider that predates the classifier returns no type_confidence — the default
    expense_receipt path must be unchanged (no manual gate)."""
    r = routing.route("expense_receipt", None)
    assert r.queue == routing.QUEUE_ECLAIM and not r.needs_manual


def test_vendor_invoice_and_do_route_to_ap_holding():
    assert routing.route("vendor_invoice", Decimal("0.9")).queue == routing.QUEUE_AP_HOLDING
    assert routing.route("delivery_order", Decimal("0.9")).queue == routing.QUEUE_AP_HOLDING


def test_low_confidence_anything_needs_a_manual_decision():
    r = routing.route("vendor_invoice", Decimal("0.40"), threshold=Decimal("0.85"))
    assert r.queue == routing.QUEUE_PENDING and r.needs_manual
    # even a would-be expense receipt, if the model is unsure, is not auto-filed
    r2 = routing.route("expense_receipt", Decimal("0.10"), threshold=Decimal("0.85"))
    assert r2.queue == routing.QUEUE_PENDING and r2.needs_manual


def test_unknown_type_needs_a_manual_decision():
    r = routing.route("unknown", Decimal("0.99"))
    assert r.queue == routing.QUEUE_PENDING and r.needs_manual


def test_link_key_needs_both_vendor_and_ref():
    assert routing.link_key("Acme Sdn Bhd", "PO-77") == "acme sdn bhd|po-77"
    assert routing.link_key("Acme", None) is None
    assert routing.link_key(None, "PO-77") is None
    assert routing.link_key("Acme", "  ") is None


# --------------------------------------------------------------------------- #
# Tolerant provider coercion (a stray classifier value never fails the read)
# --------------------------------------------------------------------------- #
def test_default_document_type_is_expense_receipt():
    assert Extraction().document_type == "expense_receipt"


def test_coerce_unknown_document_type_becomes_unknown():
    data = {"document_type": "invoice-ish", "type_signals": "not a list"}
    _coerce_classification(data)
    assert data["document_type"] == "unknown"
    # a non-list type_signals is dropped rather than raising
    assert "type_signals" not in data or isinstance(data["type_signals"], list)


def test_coerce_junk_type_confidence_to_none_does_not_kill_the_read():
    """F4: a non-numeric type_confidence ('very sure') is a str, so a bare isinstance
    check let it reach the Decimal validator and raise — killing the whole page read.
    It must be coerced to None and the extraction must then validate cleanly."""
    data = {"document_type": "vendor_invoice", "type_confidence": "very sure"}
    _coerce_classification(data)
    assert data["type_confidence"] is None
    assert Extraction.model_validate(data).type_confidence is None   # no raise


def test_coerce_keeps_a_numeric_string_confidence():
    data = {"document_type": "vendor_invoice", "type_confidence": "0.9"}
    _coerce_classification(data)
    assert Extraction.model_validate(data).type_confidence == Decimal("0.9")


def test_coerce_trims_and_caps_signals():
    data = {"document_type": "vendor_invoice", "type_signals": ["  Bill To  ", "", "x" * 500] + ["s"] * 20}
    _coerce_classification(data)
    assert "Bill To" in [s.strip() for s in data["type_signals"]]
    assert len(data["type_signals"]) <= 12
    assert all(len(s) <= 120 for s in data["type_signals"])


# --------------------------------------------------------------------------- #
# End-to-end: capture diverts vendor bills; correction re-files them
# --------------------------------------------------------------------------- #
def _capture(client, *, n=1, attested=True):
    files = [("files", (f"v{i}.png", b"\x89PNG\r\n bill", "image/png")) for i in range(n)]
    data = {"items": "[]"}
    if attested:
        data["attested"] = "yes"
    return client.post("/capture", files=files, data=data, follow_redirects=False)


def _bill(**kw) -> Extraction:
    base = dict(
        vendor="Acme Supplies", doc_no="INV-9", total_amount=Decimal("500"),
        document_type="vendor_invoice", type_confidence=Decimal("0.95"),
        type_signals=["Tax Invoice", "Payment Terms"],
    )
    base.update(kw)
    return Extraction(**base)


def test_vendor_invoice_diverts_to_holding_not_a_claim(client, db_session, fake_ocr):
    fake_ocr.extraction = _bill()
    resp = _capture(client)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/intake/holding")

    assert db_session.execute(select(Claim)).scalars().first() is None   # no claim
    intake = db_session.execute(select(DocumentIntake)).scalars().one()
    assert intake.document_type == "vendor_invoice"
    assert intake.routed_to == "ap_holding" and intake.routed_by == "system"
    assert intake.status == "open" and intake.vendor == "Acme Supplies"
    chain = Repos.for_session(db_session).audit.chain("document_intake", intake.id)
    assert any(e.event_type == "intake_routed" for e in chain)


def test_expense_receipt_still_becomes_a_claim(client, db_session, fake_ocr):
    """The dominant path is unchanged: a receipt is filed as a claim, not diverted."""
    fake_ocr.extraction = Extraction(
        vendor="Petrol Stn", total_amount=Decimal("80"),
        document_type="expense_receipt", type_confidence=Decimal("0.97"),
    )
    resp = _capture(client)
    assert resp.status_code == 303
    assert "/claims/" in resp.headers["location"]
    assert db_session.execute(select(Claim)).scalars().first() is not None
    assert db_session.execute(select(DocumentIntake)).scalars().first() is None


def test_holding_queue_lists_the_bill(client, fake_ocr):
    fake_ocr.extraction = _bill(vendor="Widget Wholesale")
    _capture(client)
    page = client.get("/intake/holding")
    assert page.status_code == 200
    assert "Widget Wholesale" in page.text
    assert "vendor invoice" in page.text


def test_reroute_to_eclaim_builds_claim_and_consumes_intake(client, db_session, fake_ocr):
    fake_ocr.extraction = _bill(total_amount=Decimal("120"))
    _capture(client)
    intake = db_session.execute(select(DocumentIntake)).scalars().one()

    resp = client.post(
        f"/intake/{intake.id}/reroute", data={"to": "eclaim"}, follow_redirects=False
    )
    assert resp.status_code == 303 and "/claims/" in resp.headers["location"]

    db_session.expire_all()
    row = db_session.get(DocumentIntake, intake.id)
    assert row.status == "consumed"
    assert row.routed_to == "eclaim" and row.routed_by == "user"
    assert row.claim_id is not None
    assert db_session.get(Claim, row.claim_id) is not None
    chain = Repos.for_session(db_session).audit.chain("document_intake", intake.id)
    assert any(e.event_type == "intake_rerouted" for e in chain)


def test_consumed_intake_cannot_be_rerouted_again(client, db_session, fake_ocr):
    fake_ocr.extraction = _bill()
    _capture(client)
    intake = db_session.execute(select(DocumentIntake)).scalars().one()
    client.post(f"/intake/{intake.id}/reroute", data={"to": "eclaim"}, follow_redirects=False)
    again = client.post(
        f"/intake/{intake.id}/reroute", data={"to": "eclaim"}, follow_redirects=False
    )
    assert again.status_code == 409


def test_holding_count_matches_the_queue(client, db_session, fake_ocr):
    """F9: the nav badge uses a COUNT that matches the holding queue length."""
    ids = db_session.info["principal"]
    fake_ocr.extraction = _bill()
    _capture(client)
    _capture(client)
    cids = frozenset({ids["client"]})
    assert intake_service.holding_count(db_session, cids) == len(
        intake_service.holding_queue(db_session, cids)
    ) == 2


def test_holding_queue_excludes_eclaim_rows(db_session):
    """A record routed to e-Claim is never in the holding queue (it became a claim)."""
    ids = db_session.info["principal"]
    intake_service.record_intake(
        db_session, firm_id=ids["firm"], client_id=ids["client"],
        created_by_user_id=ids["user"],
        extraction=Extraction(document_type="expense_receipt", type_confidence=Decimal("0.95")),
        provenance=intake_service.Provenance(), actor="t", claim_id=None,
    )
    # expense_receipt routed to eclaim → status open but routed_to == eclaim, so it is
    # NOT part of the AP/pending holding queue.
    assert intake_service.holding_queue(db_session, frozenset({ids["client"]})) == []


# --------------------------------------------------------------------------- #
# F2 — the classifier is NOT bypassed on the main (pre-read) capture flow
# --------------------------------------------------------------------------- #
def _capture_items(client, files, items):
    return client.post(
        "/capture",
        files=[("files", f) for f in files],
        data={"items": json.dumps(items), "attested": "yes"},
        follow_redirects=False,
    )


def test_preread_vendor_bill_diverts_through_the_main_ui(client, db_session):
    """A vendor bill dropped through the normal capture UI (pre-read via
    /capture/extract, posted as an item WITH data + document_type) must divert to the
    holding queue — not be silently forced into e-Claim (the F2 bug)."""
    resp = _capture_items(
        client,
        [("v.png", b"\x89PNG\r\n bill", "image/png")],
        [{"vendor": "Acme", "total_amount": "500",
          "document_type": "vendor_invoice", "type_confidence": "0.95"}],
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/intake/holding")
    assert db_session.execute(select(Claim)).scalars().first() is None
    intake = db_session.execute(select(DocumentIntake)).scalars().one()
    assert intake.document_type == "vendor_invoice" and intake.routed_to == "ap_holding"


def test_manual_entry_without_a_verdict_still_files_a_claim(client, db_session):
    """A purely manual entry (no classifier verdict) defaults to expense_receipt and is
    filed as a claim — the dominant path is unchanged."""
    resp = _capture_items(
        client,
        [("r.png", b"\x89PNG\r\n rc", "image/png")],
        [{"vendor": "Cafe", "total_amount": "20", "expense_type": "other"}],
    )
    assert resp.status_code == 303 and "/claims/" in resp.headers["location"]
    assert db_session.execute(select(Claim)).scalars().first() is not None
    assert db_session.execute(select(DocumentIntake)).scalars().first() is None


def test_mixed_capture_files_receipt_and_diverts_bill_with_banner(client, db_session):
    resp = _capture_items(
        client,
        [("r.png", b"\x89PNG\r\n a", "image/png"), ("v.png", b"\x89PNG\r\n b", "image/png")],
        [
            {"vendor": "Cafe", "total_amount": "20", "expense_type": "other",
             "document_type": "expense_receipt", "type_confidence": "0.9"},
            {"vendor": "Acme", "total_amount": "500",
             "document_type": "vendor_invoice", "type_confidence": "0.95"},
        ],
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert "/claims/" in loc and "diverted=1" in loc          # the banner is fed a count
    page = client.get(loc)
    assert "Vendor bills" in page.text and "vendor bill" in page.text   # banner rendered


def test_api_upload_refuses_a_confident_vendor_bill(client, fake_ocr):
    """/api/claims/upload is the staff-expense endpoint — a confident vendor bill is
    refused (422), never silently filed as a claim (F2)."""
    fake_ocr.extraction = _bill()
    r = client.post("/api/claims/upload", files={"file": ("v.png", b"\x89PNG\r\n bill", "image/png")})
    assert r.status_code == 422
    assert "vendor_invoice" in r.json()["detail"]


def test_delivery_order_links_to_its_matching_invoice(db_session):
    ids = db_session.info["principal"]
    do, _ = intake_service.record_intake(
        db_session, firm_id=ids["firm"], client_id=ids["client"],
        created_by_user_id=ids["user"],
        extraction=Extraction(
            vendor="Acme", po_ref="PO-77", document_type="delivery_order",
            type_confidence=Decimal("0.95"),
        ),
        provenance=intake_service.Provenance(), actor="t",
    )
    inv, _ = intake_service.record_intake(
        db_session, firm_id=ids["firm"], client_id=ids["client"],
        created_by_user_id=ids["user"],
        extraction=Extraction(
            vendor="Acme", po_ref="PO-77", document_type="vendor_invoice",
            type_confidence=Decimal("0.95"),
        ),
        provenance=intake_service.Provenance(), actor="t",
    )
    db_session.expire_all()
    assert db_session.get(DocumentIntake, do.id).linked_intake_id == inv.id
    assert db_session.get(DocumentIntake, inv.id).linked_intake_id == do.id
