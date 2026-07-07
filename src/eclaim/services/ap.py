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
from .sod import SoDViolation, _describe_rule, _rule_satisfied, select_matrix_rule

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


def create_from_intake(
    session: Session, *, intake: DocumentIntake, actor: str,
) -> ApInvoice:
    """Convenience: build an AP invoice from a captured vendor-bill intake row (C1→C2),
    resolving/creating the vendor and seeding one line for the whole amount, then
    marking the intake consumed. The reviewer codes the lines afterwards."""
    vendor = get_or_create_vendor(
        session, firm_id=intake.firm_id, client_id=intake.client_id,
        name=intake.vendor or "Unknown vendor",
    )
    invoice = create_invoice(
        session, firm_id=intake.firm_id, client_id=intake.client_id,
        created_by_user_id=intake.created_by_user_id, vendor_id=vendor.id, actor=actor,
        doc_no=intake.doc_no, currency=intake.currency or "MYR",
        total_amount=intake.total_amount, po_ref=None,
        image_sha256=intake.image_sha256, image_path=intake.image_path, intake_id=intake.id,
        lines=[LineInput(description=(intake.doc_no or "Vendor bill"), line_total=intake.total_amount)],
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
) -> ApInvoice:
    """Apply accounting coding (GL / tax / carbon category / cost dims) to one line and
    record the coder on the invoice (the maker, for the SoD check at approval). Only
    while the invoice is still pre-approval."""
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
    ):
        if value is not None:
            setattr(line, field, value)
    invoice.coded_by_user_id = coder.user_id
    if invoice.status == "captured":
        invoice.status = "coded"
    _audit(session, invoice, "ap_coded", actor, {"line_id": str(line_id)})
    session.flush()
    return invoice


def submit_for_approval(session: Session, *, invoice_id: uuid.UUID, actor: str) -> ApInvoice:
    invoice = get_invoice(session, invoice_id)
    if invoice.status not in ("coded", "captured"):
        raise IllegalApTransition(
            f"only a coded invoice can be sent for approval (status {invoice.status!r})"
        )
    invoice.status = "pending_approval"
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
    viewers, must hold the client grant, the coder cannot self-approve (maker≠checker),
    within their authority limit, and must satisfy the ``ap`` approval-matrix band."""
    if approver.base_role == "viewer":
        raise SoDViolation("viewers cannot approve invoices")
    if not approver.can_access_client(invoice.client_id):
        raise SoDViolation("approver has no grant to this client")
    if invoice.coded_by_user_id is not None and invoice.coded_by_user_id == approver.user_id:
        raise SoDViolation("the user who coded an invoice cannot approve it")
    amount = invoice.total_amount
    if (
        amount is not None and approver.authority_limit is not None
        and amount > approver.authority_limit
    ):
        raise SoDViolation(
            f"amount {amount} exceeds approver authority limit {approver.authority_limit}"
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
    return select_matrix_rule(
        rules, amount=invoice.total_amount if invoice.total_amount is not None else Decimal(0),
        department=None, category_ids=cats, module="ap",
    )


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
