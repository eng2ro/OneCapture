"""Parent-document reference on the CarbonNext handoff (Appendix F-B).

The carbon unit is the LINE, not the document — a bill can hold carbon and non-carbon
lines, so the forwarded amount is legitimately LESS than the document total. Every
handoff row now carries ``doc_no`` + ``doc_gross_total`` so CarbonNext/an auditor can
reconcile by REFERENCE (which bill, and why less than its total), never by totals.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from eclaim.auth.principal import Principal
from eclaim.db.models import ApInvoice, Category, CarbonHandoff, Vendor
from eclaim.ocr.base import Extraction
from eclaim.services import ap as ap_service
from eclaim.services import coverage as coverage_service
from eclaim.services.claims import ClaimService, Repos, _doc_gross_totals, _doc_key


def _p(ids) -> Principal:
    return Principal(
        user_id=ids["user"], firm_id=ids["firm"], base_role="partner",
        allowed_client_ids=frozenset({ids["client"]}), email="partner@seed.test",
    )


def _cat(db_session, expense_type) -> Category:
    ids = db_session.info["principal"]
    return db_session.execute(
        select(Category).where(
            Category.client_id == ids["client"], Category.expense_type == expense_type
        )
    ).scalar_one()


def _noncarbon_cat(db_session) -> Category:
    ids = db_session.info["principal"]
    c = Category(
        firm_id=ids["firm"], client_id=ids["client"],
        name="Bank charges", expense_type="bank_charge", carbon_relevant=False,
    )
    db_session.add(c)
    db_session.flush()
    return c


def _add(svc, repos, claim, fake_ocr, tmp_path, *, doc_no, total, expense_type, category_id):
    fake_ocr.extraction = Extraction(
        vendor="Shell", doc_no=doc_no, total_amount=Decimal(total),
        expense_type=expense_type, quantity=Decimal("10"), unit="L",
    )
    return svc.add_line(
        repos=repos, claim=claim, image_bytes=b"\x89PNG " + doc_no.encode(),
        media_type="image/png", ocr=fake_ocr, image_dir=tmp_path,
        category_id=category_id, payment_method="corporate_card",   # no attestation gate
    )


def _released_split_doc(client, fake_ocr, db_session, tmp_path):
    """One claim, one SOURCE document (doc_no INV5) split into a carbon line (RM300)
    and a non-carbon line (RM200), approved + released."""
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    claim = svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])
    _add(svc, repos, claim, fake_ocr, tmp_path, doc_no="INV5", total="300",
         expense_type="fuel_diesel", category_id=_cat(db_session, "fuel_diesel").id)
    # A non-carbon line on the SAME document: carbon_relevant comes from the assigned
    # category (carbon_relevant=False), not the (Literal-constrained) expense_type.
    _add(svc, repos, claim, fake_ocr, tmp_path, doc_no="INV5", total="200",
         expense_type="other", category_id=_noncarbon_cat(db_session).id)
    partner = _p(ids)
    svc.approve(repos=repos, claim_id=claim.id, actor="p", approver=partner)
    svc.release(repos=repos, claim_id=claim.id, actor="p", principal=partner)
    return claim, partner


def _forwards(db_session, claim_id):
    return db_session.execute(
        select(CarbonHandoff).where(
            CarbonHandoff.claim_id == claim_id, CarbonHandoff.direction == "forward"
        )
    ).scalars().all()


# --------------------------------------------------------------------------- #
# Release stamps the reference; gross exceeds the forwarded amount
# --------------------------------------------------------------------------- #
def test_release_stamps_doc_ref_and_gross_exceeds_forwarded(client, fake_ocr, db_session, tmp_path):
    claim, _ = _released_split_doc(client, fake_ocr, db_session, tmp_path)
    forwards = _forwards(db_session, claim.id)

    assert len(forwards) == 1                          # only the carbon line forwards
    h = forwards[0]
    assert h.doc_no == "INV5"
    assert h.doc_gross_total == Decimal("500.00")      # 300 carbon + 200 non-carbon
    assert h.amount == Decimal("300.00")               # forwarded = the carbon line only
    assert h.doc_gross_total > h.amount, "gross must reveal the non-carbon remainder"


def test_single_receipt_gross_equals_line_total(client, fake_ocr, db_session, tmp_path):
    svc, repos = ClaimService(), Repos.for_session(db_session)
    ids = db_session.info["principal"]
    claim = svc.start_claim(repos=repos, firm_id=ids["firm"], client_id=ids["client"])
    _add(svc, repos, claim, fake_ocr, tmp_path, doc_no="INV9", total="300",
         expense_type="fuel_diesel", category_id=_cat(db_session, "fuel_diesel").id)
    partner = _p(ids)
    svc.approve(repos=repos, claim_id=claim.id, actor="p", approver=partner)
    svc.release(repos=repos, claim_id=claim.id, actor="p", principal=partner)

    h = _forwards(db_session, claim.id)[0]
    assert h.doc_no == "INV9"
    assert h.doc_gross_total == Decimal("300.00") == h.amount   # fully carbon


def test_reverse_carries_same_positive_gross(client, fake_ocr, db_session, tmp_path):
    """A reversal reconciles to the SAME bill: doc_no + doc_gross_total unchanged and
    POSITIVE (document context, not a signed amount), while the amount is negated."""
    claim, partner = _released_split_doc(client, fake_ocr, db_session, tmp_path)
    svc, repos = ClaimService(), Repos.for_session(db_session)
    svc.reverse(repos=repos, claim_id=claim.id, actor="p", principal=partner)

    rev = db_session.execute(
        select(CarbonHandoff).where(
            CarbonHandoff.claim_id == claim.id, CarbonHandoff.direction == "reversal"
        )
    ).scalars().all()
    assert len(rev) == 1
    assert rev[0].doc_no == "INV5"
    assert rev[0].doc_gross_total == Decimal("500.00")     # positive, same bill
    assert rev[0].amount == Decimal("-300.00")             # amount negated


# --------------------------------------------------------------------------- #
# Pure grouping helper
# --------------------------------------------------------------------------- #
class _Ln:
    def __init__(self, id, doc_no, total):
        self.id = id
        self.doc_no = doc_no
        self.total_amount = None if total is None else Decimal(total)


def test_doc_gross_totals_groups_by_doc_no_and_isolates_nulls():
    a, b, c, d = _Ln("1", "INV5", "300"), _Ln("2", "INV5", "200"), _Ln("3", None, "50"), _Ln("4", None, "10")
    totals = _doc_gross_totals([a, b, c, d])
    assert totals[_doc_key(a)] == Decimal("500")          # INV5 lines summed
    assert totals[_doc_key(b)] == Decimal("500")          # same doc → same total
    assert totals[_doc_key(c)] == Decimal("50")           # blank-doc lines stay separate
    assert totals[_doc_key(d)] == Decimal("10")
    assert _doc_key(c) != _doc_key(d)                     # never merged into one phantom doc


# --------------------------------------------------------------------------- #
# AP handoff uses the identical field contract (design)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Coverage view (F-B2): captured spend vs carbon-forwarded, drill-down
# --------------------------------------------------------------------------- #
def test_coverage_report_shows_captured_vs_forwarded(client, fake_ocr, db_session, tmp_path):
    claim, _ = _released_split_doc(client, fake_ocr, db_session, tmp_path)
    ids = db_session.info["principal"]
    periods = coverage_service.coverage_report(db_session, frozenset({ids["client"]}))

    assert len(periods) == 1
    p = periods[0]
    assert p.captured == Decimal("500.00") and p.forwarded == Decimal("300.00")
    assert p.coverage_pct == 60
    assert p.doc_count == 1 and p.line_count == 1

    d = p.documents[0]
    assert d.doc_no == "INV5"
    assert d.captured == Decimal("500.00") and d.forwarded == Decimal("300.00")
    assert d.uncovered == Decimal("200.00")        # the non-carbon remainder, surfaced
    assert d.coverage_pct == 60
    assert len(d.lines) == 1                        # drill-down to the forwarded line


def test_coverage_nets_out_reversals(client, fake_ocr, db_session, tmp_path):
    claim, partner = _released_split_doc(client, fake_ocr, db_session, tmp_path)
    svc, repos = ClaimService(), Repos.for_session(db_session)
    svc.reverse(repos=repos, claim_id=claim.id, actor="p", principal=partner)
    ids = db_session.info["principal"]

    d = coverage_service.coverage_report(db_session, frozenset({ids["client"]}))[0].documents[0]
    assert d.forwarded == Decimal("0.00")          # forward 300 + reversal -300
    assert d.captured == Decimal("500.00")         # captured (the bill) is unchanged
    assert d.coverage_pct == 0


def test_coverage_period_filter(client, fake_ocr, db_session, tmp_path):
    claim, _ = _released_split_doc(client, fake_ocr, db_session, tmp_path)
    ids = db_session.info["principal"]
    only = coverage_service.coverage_report(db_session, frozenset({ids["client"]}))[0].period
    assert coverage_service.coverage_report(db_session, frozenset({ids["client"]}), period=only)
    assert coverage_service.coverage_report(db_session, frozenset({ids["client"]}), period="1999-01") == []


def test_coverage_page_renders_the_difference(client, fake_ocr, db_session, tmp_path):
    _released_split_doc(client, fake_ocr, db_session, tmp_path)
    page = client.get("/coverage")
    assert page.status_code == 200
    assert "Carbon coverage" in page.text
    assert "INV5" in page.text
    assert "500.00" in page.text and "300.00" in page.text   # captured + forwarded shown


def test_ap_handoff_doc_fields_are_the_invoice_doc_and_gross(client, db_session):
    ids = db_session.info["principal"]
    vendor = Vendor(firm_id=ids["firm"], client_id=ids["client"], name="Acme")
    db_session.add(vendor)
    db_session.flush()
    inv = ApInvoice(
        firm_id=ids["firm"], client_id=ids["client"], vendor_id=vendor.id,
        doc_no="AP-77", total_amount=Decimal("900.00"), idempotency_key="k1",
    )
    db_session.add(inv)
    db_session.flush()
    assert ap_service.handoff_doc_fields(inv) == ("AP-77", Decimal("900.00"))
