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


def test_quotation_and_po_route_to_holding_for_reference():
    """A quotation / purchase order is AP-side context — captured in the holding queue,
    never e-Claim, but not a payable bill."""
    assert routing.route("quotation", Decimal("0.95")).queue == routing.QUEUE_AP_HOLDING
    assert routing.route("purchase_order", Decimal("0.95")).queue == routing.QUEUE_AP_HOLDING


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


def test_coerce_nonfinite_confidence_to_none_does_not_kill_the_read():
    """F4 follow-up: NaN/Infinity parse as valid Decimals (json.loads even emits bare
    NaN), then pydantic's finite check raises — the same whole-read crash by another
    door. They must degrade to None, per field, without failing validation."""
    for junk in ("NaN", "Infinity", "-inf", float("nan")):
        data = {"document_type": "vendor_invoice", "type_confidence": junk}
        _coerce_classification(data)
        assert data["type_confidence"] is None, junk
        assert Extraction.model_validate(data).type_confidence is None


def test_coerce_junk_tax_fields_never_kill_the_read():
    """Tax extraction (d9a32ed) must use the same tolerant coercion as the classifier
    fields: a model emitting tax_amount='6%' (the prompt mentions 6% SST) previously
    raised in the Decimal validator and lost the whole page."""
    data = {"document_type": "expense_receipt", "tax_amount": "6%", "tax_code": "  SR  "}
    _coerce_classification(data)
    assert data["tax_amount"] is None
    assert data["tax_code"] == "SR"
    assert Extraction.model_validate(data).tax_amount is None


def test_coerce_unknown_unit_to_none_does_not_kill_the_read():
    """unit is a strict Literal — a model answering 'gal'/'litre' previously raised
    and lost the whole page (F4 class). Unknown units drop to None (quantity
    survives for the reviewer); case variants normalize; kg is now valid."""
    for junk in ("gal", "litre", "units"):
        data = {"unit": junk, "quantity": "10"}
        _coerce_classification(data)
        assert data["unit"] is None, junk
        assert Extraction.model_validate(data).quantity == Decimal("10")
    for variant, want in (("KG", "kg"), ("l", "L"), ("KWH", "kWh"), ("kg", "kg")):
        data = {"unit": variant}
        _coerce_classification(data)
        assert data["unit"] == want, variant
        assert Extraction.model_validate(data).unit == want


def test_coerce_drops_impossible_tax():
    # negative tax, and tax exceeding the document gross, are OCR errors — dropped so
    # the derived net can never go negative without a human keying it deliberately.
    neg = {"tax_amount": "-5", "total_amount": "100"}
    _coerce_classification(neg)
    assert neg["tax_amount"] is None and neg["total_amount"] == "100"
    over = {"tax_amount": "150.00", "total_amount": "100.00"}
    _coerce_classification(over)
    assert over["tax_amount"] is None


def test_extraction_from_item_tolerates_junk_numerics():
    """A crafted POST (or stale tab) with junk numeric strings must degrade to None
    per field, not 500 via decimal.InvalidOperation."""
    from eclaim.services.ingestion import extraction_from_item

    ext = extraction_from_item(
        {"vendor": "V", "total_amount": "abc", "tax_amount": "NaN",
         "quantity": "1/2", "type_confidence": "sure"}
    )
    assert ext.vendor == "V"
    assert ext.total_amount is None and ext.tax_amount is None
    assert ext.quantity is None and ext.type_confidence is None


def test_coerce_trims_and_caps_signals():
    data = {"document_type": "vendor_invoice", "type_signals": ["  Bill To  ", "", "x" * 500] + ["s"] * 20}
    _coerce_classification(data)
    assert "Bill To" in [s.strip() for s in data["type_signals"]]
    assert len(data["type_signals"]) <= 12
    assert all(len(s) <= 120 for s in data["type_signals"])


# --------------------------------------------------------------------------- #
# End-to-end: capture diverts vendor bills; correction re-files them
# --------------------------------------------------------------------------- #
def _capture(client, *, n=1, attested=True, marker=b""):
    # ``marker`` distinguishes the image bytes so distinct uploads don't dedup by sha.
    files = [
        ("files", (f"v{i}.png", b"\x89PNG\r\n bill" + marker + bytes([i]), "image/png"))
        for i in range(n)
    ]
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


def test_quotation_only_capture_needs_no_attestation(client, db_session):
    """A quotation (or PO) creates NO reimbursement — demanding the 'paid with my own
    money' tick for it would compel a false declaration. A pre-read quotation-only
    capture must divert without the tick."""
    import json

    files = [("files", ("q.png", b"\x89PNG\r\n quote", "image/png"))]
    items = [{
        "vendor": "Acme Supplies", "doc_no": "Q-77", "total_amount": "1200",
        "document_type": "quotation", "type_confidence": "0.95",
        "type_signals": ["Quotation", "Valid until"],
    }]
    resp = client.post("/capture", files=files, data={"items": json.dumps(items)},
                       follow_redirects=False)          # note: NO attested field
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/intake/holding")
    intake = db_session.execute(select(DocumentIntake)).scalars().one()
    assert intake.document_type == "quotation"
    assert db_session.execute(select(Claim)).scalars().first() is None


def test_capture_page_js_carries_verdict_and_exempts_non_expense_types(client):
    """Template pin (F2 residual): the capture page's item payload must carry the
    classifier verdict, and its attestation gate must key on expense_receipt — not a
    denylist that forgets new types (quotation/PO forced a false tick)."""
    page = client.get("/capture")
    assert page.status_code == 200
    assert "document_type" in page.text            # verdict rides the items payload
    assert 'dt === "expense_receipt"' in page.text  # allowlist gate, not a denylist


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
    _capture(client, marker=b"A")           # two DISTINCT images (else they'd dedup)
    _capture(client, marker=b"B")
    cids = frozenset({ids["client"]})
    assert intake_service.holding_count(db_session, cids) == len(
        intake_service.holding_queue(db_session, cids)
    ) == 2


def test_reuploading_the_same_bill_does_not_duplicate_the_holding_row(client, db_session, fake_ocr):
    """Re-capturing the IDENTICAL file (same image sha256) must not pile up duplicate
    holding rows — the queue de-dups on the image. Fixes the '6 identical rows' case."""
    fake_ocr.extraction = _bill()
    _capture(client)
    _capture(client)                        # same bytes → same sha → deduped
    _capture(client)
    rows = db_session.execute(
        select(DocumentIntake).where(DocumentIntake.status == "open")
    ).scalars().all()
    assert len(rows) == 1


def test_suppressed_duplicate_capture_is_audited(client, db_session, fake_ocr):
    """A suppressed re-upload must never be invisible: the surviving row's audit
    chain records each suppressed attempt."""
    fake_ocr.extraction = _bill()
    _capture(client)
    _capture(client)
    intake = db_session.execute(select(DocumentIntake)).scalars().one()
    chain = Repos.for_session(db_session).audit.chain("document_intake", intake.id)
    assert sum(1 for e in chain if e.event_type == "intake_duplicate_suppressed") == 1


def test_confident_recapture_upgrades_a_stale_pending_row(db_session):
    """357d0f3 finding: the old dedup returned the STALE row — a re-capture of a
    previously-unclassifiable bill (now confidently read, e.g. after the OCR cache
    was swept or the threshold changed) was told it went to Vendor bills while the
    row still sat in 'pending'. A confident re-read must upgrade the surviving row.
    Service-level: the web path reuses cached OCR for identical bytes, so the fresh
    verdict arrives here only in the cache-expired / re-photo case."""
    ids = db_session.info["principal"]
    prov = intake_service.Provenance(sha256="samesha", name="bill.png")

    def _rec(conf):
        return intake_service.record_intake(
            db_session, firm_id=ids["firm"], client_id=ids["client"],
            created_by_user_id=ids["user"],
            extraction=_bill(type_confidence=Decimal(conf)),
            provenance=prov, actor="t",
        )

    stale, _ = _rec("0.30")                       # below threshold → pending
    assert stale.routed_to == "pending" and stale.needs_manual

    row, decision = _rec("0.95")                  # confident re-read, same sha
    assert row.id == stale.id                     # deduped to the surviving row
    assert row.routed_to == "ap_holding" == decision.queue   # upgraded, not stale
    assert row.type_confidence == Decimal("0.95") and not row.needs_manual
    chain = Repos.for_session(db_session).audit.chain("document_intake", row.id)
    dup = [e for e in chain if e.event_type == "intake_duplicate_suppressed"]
    assert dup and dup[-1].detail["reclassified"] is True


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


def test_vendor_bill_only_capture_needs_no_attestation(client, db_session):
    """A pure vendor-bill upload diverts to the holding queue and creates no
    reimbursement claim — so it must submit WITHOUT the out-of-pocket declaration."""
    resp = client.post(
        "/capture",
        files=[("files", ("v.png", b"\x89PNG\r\n bill", "image/png"))],
        data={"items": json.dumps([
            {"vendor": "Acme", "total_amount": "500",
             "document_type": "vendor_invoice", "type_confidence": "0.95"},
        ])},                                              # NO attested field
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/intake/holding")
    assert db_session.execute(select(Claim)).scalars().first() is None


def test_mixed_capture_without_attestation_is_blocked(client, db_session):
    """A batch that contains a real expense receipt still requires the declaration —
    the receipt is genuine out-of-pocket spend."""
    resp = client.post(
        "/capture",
        files=[("files", ("r.png", b"\x89PNG\r\n a", "image/png")),
               ("files", ("v.png", b"\x89PNG\r\n b", "image/png"))],
        data={"items": json.dumps([
            {"vendor": "Cafe", "total_amount": "20", "expense_type": "other",
             "document_type": "expense_receipt", "type_confidence": "0.9"},
            {"vendor": "Acme", "total_amount": "500",
             "document_type": "vendor_invoice", "type_confidence": "0.95"},
        ])},                                              # NO attested
        follow_redirects=False,
    )
    assert resp.status_code == 200                        # re-render, blocked
    assert "out-of-pocket declaration" in resp.text
    assert db_session.execute(select(Claim)).scalars().first() is None


def test_submission_needs_attestation_logic():
    from eclaim.web.routes import _submission_needs_attestation

    bill = {"vendor": "A", "total_amount": "5",
            "document_type": "vendor_invoice", "type_confidence": "0.95"}
    receipt = {"vendor": "B", "total_amount": "5",
               "document_type": "expense_receipt", "type_confidence": "0.9"}
    trip = {"origin": "KL", "destination": "PJ"}

    assert _submission_needs_attestation([bill], [], 1) is False       # all vendor bills
    assert _submission_needs_attestation([bill, bill], [], 2) is False
    assert _submission_needs_attestation([receipt], [], 1) is True     # an expense
    assert _submission_needs_attestation([bill, receipt], [], 2) is True   # mixed
    assert _submission_needs_attestation([], [trip], 0) is True        # mileage
    assert _submission_needs_attestation([], [], 1) is True            # an unread file


def test_quotation_is_captured_but_not_fileable_as_ap(client, db_session, fake_ocr):
    """A quotation lands in the holding queue labelled correctly, the UI marks it 'not
    payable' (no File-as-AP button), and the file-ap action is refused server-side."""
    fake_ocr.extraction = _bill(document_type="quotation")
    _capture(client)
    intake = db_session.execute(select(DocumentIntake)).scalars().one()
    assert intake.document_type == "quotation" and intake.routed_to == "ap_holding"

    page = client.get("/intake/holding").text
    assert "not payable" in page

    r = client.post(f"/intake/{intake.id}/file-ap", follow_redirects=False)
    assert r.status_code == 400
    assert db_session.get(DocumentIntake, intake.id).status == "open"   # not consumed


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
