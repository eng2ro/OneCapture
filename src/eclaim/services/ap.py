"""Accounts-payable domain service (C2): vendor bills finance pays.

A vendor bill the classifier diverted (C1) becomes a coded, approved, exportable AP
invoice here. Workflow first, ERP posting stubbed (see :mod:`eclaim.services.erp`):

``captured → coded → pending_approval → approved → posted → paid`` (+ ``held`` for a
suspected duplicate, ``rejected``). Separation of duties is enforced at BOTH the DB
(``ck_ap_invoice_sod``) and here: whoever CODED the invoice cannot APPROVE it. The
approval band reuses the shared Appendix-B matrix engine scoped to the ``ap`` module,
so vendor bills can require different sign-off than staff claims. Duplicate detection
is HARD here (same vendor + doc_no is the classic double-payment) — a match parks the
invoice in ``held`` rather than the soft advisory flag e-Claim uses.

The service never commits — the caller owns the transaction.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..auth.principal import Principal
from ..db.models import ApInvoice, ApInvoiceLine, ApprovalMatrixRule, DocumentIntake, Vendor
from ..repositories import AuditRepository
from ..tenancy import set_tenant_context
from .audit import record_event
from .claims import ClaimError
from .sod import (
    MATRIX_NO_BAND,
    SoDViolation,
    _describe_rule,
    _matrix_gap,
    _rule_satisfied,
    select_matrix_rule,
)

ENTITY_TYPE = "ap_invoice"

# Lifecycle statuses whose existing invoice makes a new one with the same vendor+doc_no
# a suspected duplicate (a rejected bill doesn't count).
_LIVE_STATUSES = ("captured", "coded", "pending_approval", "approved", "posted", "paid", "held")


class ApError(ClaimError):
    """Base for AP-service errors (mapped to 4xx by the routes)."""


class ApNotFound(ApError):
    pass


class IllegalApTransition(ApError):
    """An action not allowed from the invoice's current status."""


@dataclass(frozen=True)
class LineInput:
    description: str | None = None
    quantity: Decimal | None = None
    uom: str | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    gl_code: str | None = None
    tax_code: str | None = None
    category_id: uuid.UUID | None = None
    department: str | None = None
    project_code: str | None = None


# --------------------------------------------------------------------------- #
# Vendor master
# --------------------------------------------------------------------------- #
def get_or_create_vendor(
    session: Session, *, firm_id: uuid.UUID, client_id: uuid.UUID, name: str,
    tax_id: str | None = None, bank_account: str | None = None,
) -> Vendor:
    """Resolve a vendor by case-insensitive name for this client, creating it if new.
    The ERP vendor code is filled later, when the vendor is mapped to the ERP."""
    clean = (name or "").strip() or "Unknown vendor"
    existing = session.execute(
        select(Vendor).where(
            Vendor.client_id == client_id, func.lower(Vendor.name) == clean.lower()
        ).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    vendor = Vendor(
        firm_id=firm_id, client_id=client_id, name=clean,
        tax_id=tax_id, bank_account=bank_account,
    )
    session.add(vendor)
    session.flush()
    return vendor


# --------------------------------------------------------------------------- #
# Duplicate detection (hard)
# --------------------------------------------------------------------------- #
def find_duplicate(
    session: Session, *, client_id: uuid.UUID, vendor_id: uuid.UUID,
    doc_no: str | None, total_amount: Decimal | None, exclude_id: uuid.UUID | None = None,
) -> ApInvoice | None:
    """A live invoice with the SAME vendor + doc_no (the classic double-payment). When
    ``total_amount`` is given it must also match, so a genuine re-bill with a different
    amount isn't falsely flagged. Returns the earliest match, or None."""
    if not doc_no:
        return None
    q = select(ApInvoice).where(
        ApInvoice.client_id == client_id,
        ApInvoice.vendor_id == vendor_id,
        ApInvoice.doc_no == doc_no,
        ApInvoice.status.in_(_LIVE_STATUSES),
    )
    if total_amount is not None:
        q = q.where(ApInvoice.total_amount == total_amount)
    if exclude_id is not None:
        q = q.where(ApInvoice.id != exclude_id)
    return session.execute(q.order_by(ApInvoice.created_at).limit(1)).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
def create_invoice(
    session: Session, *,
    firm_id: uuid.UUID, client_id: uuid.UUID, created_by_user_id: uuid.UUID | None,
    vendor_id: uuid.UUID, actor: str,
    doc_no: str | None = None, doc_date: dt.date | None = None,
    due_date: dt.date | None = None, payment_terms: str | None = None,
    currency: str = "MYR", subtotal: Decimal | None = None,
    tax_amount: Decimal | None = None, total_amount: Decimal | None = None,
    po_ref: str | None = None, do_ref: str | None = None,
    image_sha256: str | None = None, image_path: str | None = None,
    intake_id: uuid.UUID | None = None,
    lines: list[LineInput] | None = None, idempotency_key: str | None = None,
) -> ApInvoice:
    """Create an AP invoice header + lines. A suspected duplicate (same vendor+doc_no,
    same amount) is captured but parked in ``held`` with a reason, never silently
    double-paid. ``idempotency_key`` (defaulted from the source doc) blocks a second
    insert of the same document."""
    idem = idempotency_key or _default_idem(intake_id, image_sha256)
    dup = find_duplicate(
        session, client_id=client_id, vendor_id=vendor_id,
        doc_no=doc_no, total_amount=total_amount,
    )
    invoice = ApInvoice(
        firm_id=firm_id, client_id=client_id, vendor_id=vendor_id,
        doc_no=doc_no, doc_date=doc_date, due_date=due_date,
        payment_terms=payment_terms, currency=currency or "MYR",
        subtotal=subtotal, tax_amount=tax_amount, total_amount=total_amount,
        po_ref=po_ref, do_ref=do_ref,
        image_sha256=image_sha256, image_path=image_path, intake_id=intake_id,
        status="held" if dup is not None else "captured",
        hold_reason=(f"possible duplicate of invoice {dup.id}" if dup is not None else None),
        idempotency_key=idem, created_by_user_id=created_by_user_id,
    )
    session.add(invoice)
    session.flush()

    for i, ln in enumerate(lines or [], start=1):
        session.add(ApInvoiceLine(
            firm_id=firm_id, client_id=client_id, ap_invoice_id=invoice.id, line_no=i,
            description=ln.description, quantity=ln.quantity, uom=ln.uom,
            unit_price=ln.unit_price, line_total=ln.line_total, gl_code=ln.gl_code,
            tax_code=ln.tax_code, category_id=ln.category_id,
            department=ln.department, project_code=ln.project_code,
        ))
    session.flush()

    _audit(session, invoice, "ap_captured", actor, {
        "status": invoice.status, "vendor_id": str(vendor_id), "doc_no": doc_no,
        "total": None if total_amount is None else str(total_amount),
        "duplicate_of": (str(dup.id) if dup is not None else None),
    })
    return invoice


# Document types that are AP-side context but are NOT payable bills — a quotation is a
# price offer, a purchase order is an order, a delivery order alone isn't payable. Only
# a vendor_invoice (or an unconfirmed unknown page the reviewer is deciding on) becomes
# an AP invoice.
_NOT_PAYABLE = ("quotation", "purchase_order", "delivery_order")


def create_from_intake(
    session: Session, *, intake: DocumentIntake, actor: str,
) -> ApInvoice:
    """Convenience: build an AP invoice from a captured vendor-bill intake row (C1→C2),
    resolving/creating the vendor and seeding one line for the whole amount, then
    marking the intake consumed. The reviewer codes the lines afterwards.

    Refuses a page the classifier read as not-payable (quotation / purchase_order /
    delivery_order): filing a quote as a payable invoice would be a real error."""
    if intake.status != "open":
        # Already filed (double click / retry / two reviewers) — a friendly refusal
        # instead of the uncaught unique-constraint IntegrityError (idempotency key
        # 'intake:{id}') that otherwise 500s.
        raise ApError("this document has already been filed as an AP invoice")
    if intake.document_type in _NOT_PAYABLE:
        raise ApError(
            f"a {intake.document_type.replace('_', ' ')} is not a payable bill — "
            "it cannot be filed as an AP invoice"
        )
    vendor = get_or_create_vendor(
        session, firm_id=intake.firm_id, client_id=intake.client_id,
        name=intake.vendor or "Unknown vendor",
    )
    from .claims import parse_receipt_date

    invoice = create_invoice(
        session, firm_id=intake.firm_id, client_id=intake.client_id,
        created_by_user_id=intake.created_by_user_id, vendor_id=vendor.id, actor=actor,
        doc_no=intake.doc_no, doc_date=parse_receipt_date(intake.doc_date),
        currency=intake.currency or "MYR",
        tax_amount=intake.tax_amount,
        total_amount=intake.total_amount, po_ref=None,
        image_sha256=intake.image_sha256, image_path=intake.image_path, intake_id=intake.id,
        # The seeded line carries the OCR's activity read (litres/kWh/kg) so the
        # carbon datum survives filing — the coder refines it, never re-keys it.
        lines=[LineInput(
            description=(intake.doc_no or "Vendor bill"),
            line_total=intake.total_amount,
            quantity=intake.quantity, uom=intake.unit,
            tax_code=intake.tax_code,
        )],
    )
    intake.status = "consumed"
    session.flush()
    return invoice


# --------------------------------------------------------------------------- #
# Code
# --------------------------------------------------------------------------- #
def code_line(
    session: Session, *, line_id: uuid.UUID, coder: Principal, actor: str,
    gl_code: str | None = None, tax_code: str | None = None,
    category_id: uuid.UUID | None = None, department: str | None = None,
    project_code: str | None = None,
    description: str | None = None, quantity: Decimal | None = None,
    uom: str | None = None, line_total: Decimal | None = None,
) -> ApInvoice:
    """Apply accounting coding (GL / tax / carbon category / cost dims) AND correct the
    line's substance (description / quantity / uom / amount — the OCR read the coder
    verifies, F-E item 12) on one line; record the coder on the invoice (the maker,
    for the SoD check at approval). Only while the invoice is still pre-approval.

    Assigning a category SNAPSHOTS its ``carbon_relevant`` onto the line (like
    claim_line at classify time) so a later category toggle never rewrites which
    lines forward to CarbonNext."""
    line = session.get(ApInvoiceLine, line_id)
    if line is None:
        raise ApNotFound(str(line_id))
    invoice = session.get(ApInvoice, line.ap_invoice_id)
    _require_writer(coder, invoice)
    if invoice.status not in ("captured", "coded", "held"):
        raise IllegalApTransition(f"cannot code an invoice in status {invoice.status!r}")

    for field, value in (
        ("gl_code", gl_code), ("tax_code", tax_code), ("category_id", category_id),
        ("department", department), ("project_code", project_code),
        ("description", description), ("quantity", quantity),
        ("uom", uom), ("line_total", line_total),
    ):
        if value is not None:
            setattr(line, field, value)
    if category_id is not None:
        from ..db.models import Category

        cat = session.get(Category, category_id)
        line.carbon_relevant = bool(cat.carbon_relevant) if cat is not None else None
    invoice.coded_by_user_id = coder.user_id
    if invoice.status == "captured":
        invoice.status = "coded"
    _audit(session, invoice, "ap_coded", actor, {"line_id": str(line_id)})
    session.flush()
    return invoice


def edit_header(
    session: Session, *, invoice_id: uuid.UUID, editor: Principal, actor: str,
    doc_no: str | None = None, doc_date=None, total_amount: Decimal | None = None,
    currency: str | None = None, po_ref: str | None = None, do_ref: str | None = None,
    vendor_name: str | None = None,
) -> ApInvoice:
    """Correct the OCR-read header fields (F-E item 12) — pre-approval only. Every
    field here feeds either the CarbonNext handoff (doc_no/doc_gross_total/currency/
    date) or the duplicate-payment control, so a misread MUST be correctable.

    After a doc_no/amount correction the duplicate check RE-RUNS: a bill whose
    corrected identity now collides with an existing one is put on hold (the
    original misread would otherwise have defeated the double-pay control).

    ``vendor_name`` renames the vendor master row ONLY while this invoice is the
    vendor's sole bill (a fresh mint from the misread) — renaming an established
    vendor would rewrite other bills' history and needs an admin decision."""
    invoice = get_invoice(session, invoice_id)
    _require_writer(editor, invoice)
    if invoice.status not in ("captured", "coded", "held"):
        raise IllegalApTransition(
            f"cannot edit the header of an invoice in status {invoice.status!r}"
        )
    changed: dict = {}
    for field, value in (
        ("doc_no", doc_no), ("doc_date", doc_date), ("total_amount", total_amount),
        ("currency", currency), ("po_ref", po_ref), ("do_ref", do_ref),
    ):
        if value is not None and getattr(invoice, field) != value:
            changed[field] = {"from": str(getattr(invoice, field)), "to": str(value)}
            setattr(invoice, field, value)

    if vendor_name is not None and vendor_name.strip():
        vendor = session.get(Vendor, invoice.vendor_id)
        if vendor is not None and vendor.name != vendor_name.strip():
            others = session.execute(
                select(func.count()).select_from(ApInvoice).where(
                    ApInvoice.vendor_id == vendor.id, ApInvoice.id != invoice.id
                )
            ).scalar_one()
            if others:
                raise ApError(
                    f"vendor {vendor.name!r} has {others} other bill(s) — renaming it "
                    "would rewrite their history; ask an admin to manage the vendor master"
                )
            changed["vendor_name"] = {"from": vendor.name, "to": vendor_name.strip()}
            vendor.name = vendor_name.strip()

    if ("doc_no" in changed or "total_amount" in changed) and invoice.status != "held":
        dup = find_duplicate(
            session, client_id=invoice.client_id, vendor_id=invoice.vendor_id,
            doc_no=invoice.doc_no, total_amount=invoice.total_amount,
            exclude_id=invoice.id,
        )
        if dup is not None:
            invoice.status = "held"
            invoice.hold_reason = f"possible duplicate of invoice {dup.id} (after header correction)"

    if changed:
        _audit(session, invoice, "ap_header_edited", actor, changed)
    session.flush()
    return invoice


def add_line(
    session: Session, *, invoice_id: uuid.UUID, editor: Principal, actor: str,
    line: LineInput,
) -> ApInvoiceLine:
    """Add a line to a pre-approval invoice — how a reviewer SPLITS a lump-filed
    bill into its real lines (F-E item 11), so a mixed carbon/non-carbon vendor
    bill can forward only its carbon share (Appendix F-A)."""
    invoice = get_invoice(session, invoice_id)
    _require_writer(editor, invoice)
    if invoice.status not in ("captured", "coded", "held"):
        raise IllegalApTransition(
            f"cannot add a line to an invoice in status {invoice.status!r}"
        )
    next_no = 1 + (session.execute(
        select(func.coalesce(func.max(ApInvoiceLine.line_no), 0)).where(
            ApInvoiceLine.ap_invoice_id == invoice.id
        )
    ).scalar_one())
    row = ApInvoiceLine(
        firm_id=invoice.firm_id, client_id=invoice.client_id,
        ap_invoice_id=invoice.id, line_no=next_no,
        description=line.description, quantity=line.quantity, uom=line.uom,
        unit_price=line.unit_price, line_total=line.line_total, gl_code=line.gl_code,
        tax_code=line.tax_code, category_id=line.category_id,
        department=line.department, project_code=line.project_code,
    )
    session.add(row)
    _audit(session, invoice, "ap_line_added", actor, {"line_no": next_no})
    session.flush()
    return row


def remove_line(
    session: Session, *, line_id: uuid.UUID, editor: Principal, actor: str,
) -> ApInvoice:
    """Remove a line from a pre-approval invoice (the counterpart of add_line when
    re-splitting). The LAST line cannot be removed — an invoice always keeps at
    least one line to code."""
    line = session.get(ApInvoiceLine, line_id)
    if line is None:
        raise ApNotFound(str(line_id))
    invoice = session.get(ApInvoice, line.ap_invoice_id)
    _require_writer(editor, invoice)
    if invoice.status not in ("captured", "coded", "held"):
        raise IllegalApTransition(
            f"cannot remove a line from an invoice in status {invoice.status!r}"
        )
    count = session.execute(
        select(func.count()).select_from(ApInvoiceLine).where(
            ApInvoiceLine.ap_invoice_id == invoice.id
        )
    ).scalar_one()
    if count <= 1:
        raise ApError("an invoice keeps at least one line — edit it instead of removing it")
    _audit(session, invoice, "ap_line_removed", actor, {"line_no": line.line_no})
    session.delete(line)
    session.flush()
    return invoice


def _require_categories(session: Session, invoice: ApInvoice) -> None:
    """Carbon coding gate (F-E item 13): every line must carry a category — an
    explicit choice, including a non-carbon category for non-emitting spend —
    otherwise a carbon-relevant bill could be approved, posted and paid having
    silently contributed nothing to the CarbonNext handoff. Enforced at BOTH
    submit and approve (approve accepts a coded invoice directly)."""
    uncoded = [str(ln.line_no) for ln in _lines(session, invoice.id) if ln.category_id is None]
    if uncoded:
        raise IllegalApTransition(
            f"every line needs a category before approval (line {', '.join(uncoded)} "
            "has none) — pick a non-carbon category if the spend isn't carbon-related"
        )


def submit_for_approval(
    session: Session, *, invoice_id: uuid.UUID, actor: str,
    submitter: Principal | None = None,
) -> ApInvoice:
    """Send a CODED invoice for approval (→ ``pending_approval``). Only a coded invoice
    qualifies (F5): accepting ``captured`` let an uncoded bill reach approval with no
    coder on record, which the SoD self-approval check then couldn't catch.

    The submitter is RECORDED (``submitted_by_user_id``) and becomes a third preparer
    role the SoD gate compares at approval — filer, coder and submitter are all barred
    from approving, at the service AND via ``ck_ap_invoice_sod`` at the DB."""
    invoice = get_invoice(session, invoice_id)
    if invoice.status != "coded":
        raise IllegalApTransition(
            f"only a coded invoice can be sent for approval (status {invoice.status!r})"
        )
    _require_categories(session, invoice)
    invoice.status = "pending_approval"
    if submitter is not None:
        invoice.submitted_by_user_id = submitter.user_id
    _audit(session, invoice, "ap_submitted", actor, {})
    session.flush()
    return invoice


# --------------------------------------------------------------------------- #
# Approve (SoD + module-scoped matrix)
# --------------------------------------------------------------------------- #
def check_can_approve_invoice(
    invoice: ApInvoice, approver: Principal, *, matrix_rule: ApprovalMatrixRule | None = None
) -> None:
    """Raise :class:`SoDViolation` if ``approver`` may not approve ``invoice``: no
    viewers, must hold the client grant, the invoice must be CODED, neither the coder
    nor the filer (the preparers) may approve it (maker≠checker), within their authority
    limit, and must satisfy the ``ap`` approval-matrix band."""
    if approver.base_role == "viewer":
        raise SoDViolation("viewers cannot approve invoices")
    if not approver.can_access_client(invoice.client_id):
        raise SoDViolation("approver has no grant to this client")
    # Must be coded before approval — otherwise the coder==approver check below is
    # vacuous (coded_by is NULL) and the filer could file, skip coding, and self-approve
    # (punch-list F5). Coding sets coded_by, so a coded invoice always has a maker.
    if invoice.coded_by_user_id is None:
        raise SoDViolation("an invoice must be coded before it can be approved")
    if invoice.coded_by_user_id == approver.user_id:
        raise SoDViolation("the user who coded an invoice cannot approve it")
    if invoice.created_by_user_id is not None and invoice.created_by_user_id == approver.user_id:
        raise SoDViolation("the user who filed an invoice cannot approve it")
    if (
        invoice.submitted_by_user_id is not None
        and invoice.submitted_by_user_id == approver.user_id
    ):
        raise SoDViolation("the user who submitted an invoice for approval cannot approve it")
    amount = invoice.total_amount
    if (
        amount is not None and approver.authority_limit is not None
        and amount > approver.authority_limit
    ):
        raise SoDViolation(
            f"amount {amount} exceeds approver authority limit {approver.authority_limit}"
        )
    if matrix_rule is MATRIX_NO_BAND:
        raise SoDViolation(
            f"no AP approval band is configured for {amount} under this client's "
            "approval matrix — ask an admin to add a band covering this amount"
        )
    if matrix_rule is not None and not _rule_satisfied(matrix_rule, approver):
        raise SoDViolation(
            f"approval of {amount} requires {_describe_rule(matrix_rule)} "
            "under this client's AP approval matrix"
        )


def matrix_rule_for_invoice(session: Session, invoice: ApInvoice) -> ApprovalMatrixRule | None:
    rules = list(session.execute(
        select(ApprovalMatrixRule).where(ApprovalMatrixRule.client_id == invoice.client_id)
    ).scalars())
    if not rules:
        return None
    cats = {ln.category_id for ln in _lines(session, invoice.id) if ln.category_id}
    amt = invoice.total_amount if invoice.total_amount is not None else Decimal(0)
    rule = select_matrix_rule(
        rules, amount=amt, department=None, category_ids=cats, module="ap",
    )
    if rule is None and _matrix_gap(rules, "ap", amt):
        return MATRIX_NO_BAND     # a gap at/above the floor → deny (fail closed)
    return rule


def approve(
    session: Session, *, invoice_id: uuid.UUID, approver: Principal, actor: str,
) -> ApInvoice:
    """Approve an invoice (→ ``approved``) under the SoD + AP-matrix gate. A blocked
    attempt is audited (``ap_approval_denied``) before the 403, mirroring e-Claim."""
    invoice = get_invoice(session, invoice_id)
    if invoice.status not in ("pending_approval", "coded"):
        raise IllegalApTransition(
            f"cannot approve an invoice in status {invoice.status!r}"
        )
    _require_categories(session, invoice)
    rule = matrix_rule_for_invoice(session, invoice)
    try:
        check_can_approve_invoice(invoice, approver, matrix_rule=rule)
    except SoDViolation as exc:
        # Persist the denial in a SEPARATE short-lived transaction (blocker B5): the
        # route rolls the request transaction back on SoDViolation, so an event written
        # on ``session`` would be lost — and committing ``session`` here would flush
        # partial work. Only when the approver actually has access to the client (so the
        # write lands in that tenant's chain under RLS).
        if approver.can_access_client(invoice.client_id):
            _record_denied(session, invoice, approver, actor=actor, reason=str(exc))
        raise
    invoice.status = "approved"
    invoice.approved_by_user_id = approver.user_id
    invoice.approved_at = dt.datetime.now(dt.timezone.utc)
    _audit(session, invoice, "ap_approved", actor, {})
    session.flush()
    return invoice


def release_hold(session: Session, *, invoice_id: uuid.UUID, actor: str) -> ApInvoice:
    """Clear a duplicate hold on a false positive (F6): ``held`` → ``coded`` if the
    invoice was already coded, else ``captured``, so the bill re-enters the normal
    flow instead of being a dead end that could only be rejected. Audited, and it
    clears the hold reason. Only a held invoice can have its hold released."""
    invoice = get_invoice(session, invoice_id)
    if invoice.status != "held":
        raise IllegalApTransition(
            f"only a held invoice can have its hold released (status {invoice.status!r})"
        )
    invoice.status = "coded" if invoice.coded_by_user_id is not None else "captured"
    prior_reason = invoice.hold_reason
    invoice.hold_reason = None
    _audit(session, invoice, "ap_hold_released", actor,
           {"to": invoice.status, "cleared_reason": prior_reason})
    session.flush()
    return invoice


def mark_paid(
    session: Session, *, invoice_id: uuid.UUID, actor: str,
    payer: Principal | None = None,
) -> ApInvoice:
    """Settle the bill — the vendor has been paid (→ ``paid``, terminal). Allowed once
    the invoice is approved or posted; audited (``ap_paid``).

    Row-locked (two concurrent marks serialise — the loser sees ``paid`` and 409s
    instead of double-auditing), and settlement-SoD-gated: the user who FILED the bill
    may not also record its payment (payer ≠ maker, mirroring the approve-side rule).
    A paid invoice stays exportable/postable until it carries an ``erp_doc_entry``, so
    paying before the ERP posting never drops the bill out of the pipeline."""
    invoice = session.get(ApInvoice, invoice_id, with_for_update=True)
    if invoice is None:
        raise ApNotFound(str(invoice_id))
    if (
        payer is not None
        and invoice.created_by_user_id is not None
        and invoice.created_by_user_id == payer.user_id
    ):
        raise SoDViolation("the user who filed an invoice cannot record its payment")
    if invoice.status not in ("approved", "posted"):
        raise IllegalApTransition(
            f"cannot mark an invoice in status {invoice.status!r} as paid"
        )
    invoice.status = "paid"
    _audit(session, invoice, "ap_paid", actor, {})
    session.flush()
    return invoice


def unapprove(
    session: Session, *, invoice_id: uuid.UUID, editor: Principal, actor: str,
) -> ApInvoice:
    """Reopen an APPROVED invoice back to ``coded`` so it can be amended or
    switched — mirrors e-Claim's unapprove, and only while the bill has not left
    for another system: once posted to the ERP or paid, the data is integrated
    downstream and corrections go through the ERP's credit-note process, never a
    silent reopen here. Clears the approval (approver + timestamp) and is
    audited; reopening is not a sign-off, so no SoD self-check applies."""
    invoice = get_invoice(session, invoice_id)
    _require_writer(editor, invoice)
    if invoice.status != "approved":
        raise IllegalApTransition(
            f"cannot reopen an invoice in status {invoice.status!r}"
            + (" — it is already integrated downstream (correct via the ERP)"
               if invoice.status in ("posted", "paid") else "")
        )
    prior_approver = invoice.approved_by_user_id
    invoice.status = "coded"
    invoice.approved_by_user_id = None
    invoice.approved_at = None
    _audit(session, invoice, "ap_unapproved", actor,
           {"prior_approver": str(prior_approver) if prior_approver else None})
    session.flush()
    return invoice


def switch_to_expense(
    session: Session, *, invoice_id: uuid.UUID, editor: Principal, actor: str,
):
    """Appendix E3: a filed vendor bill that is really a STAFF EXPENSE moves to
    e-Claim — allowed only pre-approval (afterwards corrections go through
    reject/reversal). A new in_review claim is created carrying the same image
    provenance and the bill's fields; the invoice is rejected with a note. The
    switcher becomes the claim's creator, so maker≠checker carries over — they
    cannot approve what they converted. Idempotent: a rejected invoice cannot be
    switched twice."""
    from ..db.models import Claim, ClaimLine
    from .claims import ClaimService, Repos

    invoice = get_invoice(session, invoice_id)
    _require_writer(editor, invoice)
    if invoice.status not in ("captured", "coded", "held"):
        raise IllegalApTransition(
            f"cannot switch an invoice in status {invoice.status!r} to an expense — "
            "after approval, corrections go through reject/reversal"
        )
    vendor = session.get(Vendor, invoice.vendor_id)
    lump = _lines(session, invoice.id)
    src = lump[0] if lump else None

    svc, repos = ClaimService(), Repos.for_session(session)
    claim = svc.start_claim(
        repos=repos, firm_id=invoice.firm_id, client_id=invoice.client_id,
        title=f"Converted vendor bill {invoice.doc_no or ''}".strip(),
        created_by_user_id=editor.user_id,
    )
    line = ClaimLine(
        firm_id=invoice.firm_id,
        client_id=invoice.client_id,
        claim_id=claim.id,
        line_no=1,
        vendor=(vendor.name if vendor else None),
        doc_no=invoice.doc_no,
        doc_date=(invoice.doc_date.isoformat() if invoice.doc_date else None),
        currency=invoice.currency,
        total_amount=invoice.total_amount,
        tax_amount=invoice.tax_amount,
        expense_type="other",            # the reviewer classifies on the e-Claim side
        quantity=(src.quantity if src else None),
        unit=(src.uom if src else None),
        image_path=invoice.image_path,
        image_sha256=invoice.image_sha256,
    )
    svc._recompute_line_money(line)
    session.add(line)
    session.flush()
    svc._recompute_totals(claim, [line])

    invoice.status = "rejected"
    invoice.hold_reason = "converted to a staff expense claim"
    _audit(session, invoice, "ap_converted_to_expense", actor,
           {"claim_id": str(claim.id)})
    record_event(
        AuditRepository(session), firm_id=claim.firm_id, client_id=claim.client_id,
        entity_type="claim", entity_id=claim.id, event_type="converted_from_ap",
        actor=actor, detail={"ap_invoice_id": str(invoice.id)},
    )
    session.flush()
    return claim


def reject(session: Session, *, invoice_id: uuid.UUID, actor: str, reason: str | None = None) -> ApInvoice:
    invoice = get_invoice(session, invoice_id)
    if invoice.status in ("posted", "paid"):
        raise IllegalApTransition(f"cannot reject an invoice in status {invoice.status!r}")
    invoice.status = "rejected"
    invoice.hold_reason = reason
    _audit(session, invoice, "ap_rejected", actor, {"reason": reason})
    session.flush()
    return invoice


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def get_invoice(session: Session, invoice_id: uuid.UUID) -> ApInvoice:
    invoice = session.get(ApInvoice, invoice_id)
    if invoice is None:
        raise ApNotFound(str(invoice_id))
    return invoice


def list_invoices(session: Session, client_ids, *, status: str | None = None) -> list[ApInvoice]:
    if not client_ids:
        return []
    q = select(ApInvoice).where(ApInvoice.client_id.in_(client_ids))
    if status is not None:
        q = q.where(ApInvoice.status == status)
    return list(session.execute(q.order_by(ApInvoice.created_at.desc())).scalars())


def exportable_invoices(session: Session, client_ids) -> list[ApInvoice]:
    """Invoices the accountant still needs to post into the ERP: approved OR paid
    (payment and posting are independent settlement steps), and not yet carrying an
    ``erp_doc_entry``. Selecting on 'approved' alone silently dropped a
    paid-before-posted bill out of the CSV pipeline forever."""
    if not client_ids:
        return []
    q = (
        select(ApInvoice)
        .where(
            ApInvoice.client_id.in_(client_ids),
            ApInvoice.status.in_(("approved", "paid")),
            ApInvoice.erp_doc_entry.is_(None),
        )
        .order_by(ApInvoice.created_at.desc())
    )
    return list(session.execute(q).scalars())


def handoff_doc_fields(invoice: ApInvoice) -> tuple[str | None, Decimal | None]:
    """The parent-document reference fields (F-B) the AP carbon handoff will stamp onto
    every forwarded line — the SAME contract as the e-Claim handoff
    (``carbon_handoff.doc_no`` + ``doc_gross_total``), so both channels reconcile by
    reference. For an AP invoice the document IS the invoice: its ``doc_no`` and its
    gross ``total_amount`` (each ap_invoice_line forwards only its own carbon share, so
    the forwarded amount is legitimately ≤ this). Kept pure + here so the handoff wiring
    (deferred) has one source of truth. See :func:`eclaim.services.claims._doc_gross_totals`
    for the e-Claim equivalent (a claim can hold several documents; an invoice is one)."""
    return invoice.doc_no, invoice.total_amount


def _lines(session: Session, invoice_id: uuid.UUID) -> list[ApInvoiceLine]:
    return list(session.execute(
        select(ApInvoiceLine).where(ApInvoiceLine.ap_invoice_id == invoice_id)
        .order_by(ApInvoiceLine.line_no)
    ).scalars())


lines = _lines


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _record_denied(
    session: Session, invoice: ApInvoice, approver: Principal, *, actor: str, reason: str
) -> None:
    """Persist the ``ap_approval_denied`` event in its own short-lived transaction so a
    blocked attempt is durable even though the request transaction rolls back — WITHOUT
    committing the request session (which would flush partial work; blocker B5). Mirrors
    :func:`eclaim.services.sod._record_denied`: the new session shares the request
    session's bind (its own connection in prod; a savepoint under the test harness), and
    RLS is re-primed from the approver before the append-only insert."""
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False, future=True)
    audit_session = factory()
    try:
        set_tenant_context(audit_session, approver.firm_id, approver.allowed_client_ids)
        record_event(
            AuditRepository(audit_session),
            firm_id=invoice.firm_id, client_id=invoice.client_id,
            entity_type=ENTITY_TYPE, entity_id=invoice.id,
            event_type="ap_approval_denied", actor=actor, detail={"reason": reason},
        )
        audit_session.commit()
    finally:
        audit_session.close()


def _require_writer(principal: Principal, invoice: ApInvoice) -> None:
    if principal.base_role == "viewer" or not principal.can_access_client(invoice.client_id):
        raise SoDViolation("not allowed to modify this invoice")


def _default_idem(intake_id, image_sha256) -> str:
    """Idempotency identifies the SOURCE document, NOT its business fields — else a
    genuine second occurrence of the same bill (which we WANT to capture and flag as a
    duplicate) would collide on the UNIQUE before it could be held. Re-filing the SAME
    source (intake / image) is the idempotent case that must collide."""
    if intake_id is not None:
        return f"intake:{intake_id}"
    if image_sha256:
        return f"sha:{image_sha256}"
    return f"gen:{uuid.uuid4().hex}"


def _audit(session: Session, invoice: ApInvoice, event_type: str, actor: str, detail: dict) -> None:
    record_event(
        AuditRepository(session),
        firm_id=invoice.firm_id, client_id=invoice.client_id,
        entity_type=ENTITY_TYPE, entity_id=invoice.id,
        event_type=event_type, actor=actor, detail=detail,
    )
