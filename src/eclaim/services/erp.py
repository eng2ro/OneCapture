"""ERP posting seam (C3) + a manual CSV export (C2, the stub phase).

ERP posting is deliberately a STUB in this phase: this defines the clean
:class:`ERPConnector` protocol a real connector will implement (SAP B1 Service Layer,
AutoCount/SQL Account via an on-prem agent, …), and a manual **CSV export** so an
accountant can post approved AP invoices by hand from day one — proving the whole AP
workflow with zero integration risk. Same discipline as the CarbonNext seam: a new
ERP is a new connector implementing this protocol, never a customer fork.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from ..db.models import ApInvoice, Vendor
from . import ap as ap_service


@dataclass(frozen=True)
class PostResult:
    """The outcome of pushing one invoice to an ERP: the ERP's key on success, or an
    error to land the invoice in a visible retry queue (never silently lost)."""

    ok: bool
    erp_doc_entry: str | None = None
    error: str | None = None


class ERPConnector(Protocol):
    """The single seam every ERP integration implements (push-only first; pull later
    for 3-way match). Idempotency is keyed on the invoice's ``idempotency_key``."""

    def push_ap_invoice(self, invoice: ApInvoice) -> PostResult: ...
    def pull_vendors(self) -> list: ...
    def pull_gl_accounts(self) -> list: ...


# The CSV column order — one row per invoice LINE, invoice header fields repeated, so an
# accountant can import the file straight into the ERP's AP entry. A per-ERP format is a
# thin variant of this, never a fork of the workflow.
CSV_COLUMNS = [
    "invoice_id", "vendor", "vendor_erp_code", "doc_no", "doc_date", "due_date",
    "payment_terms", "currency", "invoice_total", "po_ref", "do_ref", "status",
    "line_no", "description", "quantity", "uom", "unit_price", "line_total",
    "gl_code", "tax_code", "department", "project_code",
]


def export_ap_csv(session: Session, invoices: list[ApInvoice]) -> str:
    """Render approved AP invoices to a CSV an accountant posts manually (C2 stub).

    One row per line; a headers-only invoice (no lines) still emits a single row so it
    isn't silently dropped from the export. Money/quantities are formatted from Decimal
    without float drift."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for inv in invoices:
        vendor = session.get(Vendor, inv.vendor_id)
        header = {
            "invoice_id": str(inv.id),
            "vendor": vendor.name if vendor else "",
            "vendor_erp_code": (vendor.erp_vendor_code if vendor else "") or "",
            "doc_no": inv.doc_no or "",
            "doc_date": inv.doc_date.isoformat() if inv.doc_date else "",
            "due_date": inv.due_date.isoformat() if inv.due_date else "",
            "payment_terms": inv.payment_terms or "",
            "currency": inv.currency or "",
            "invoice_total": _num(inv.total_amount),
            "po_ref": inv.po_ref or "",
            "do_ref": inv.do_ref or "",
            "status": inv.status,
        }
        rows = ap_service.lines(session, inv.id)
        if not rows:
            writer.writerow(header)
            continue
        for ln in rows:
            writer.writerow({
                **header,
                "line_no": ln.line_no,
                "description": ln.description or "",
                "quantity": _num(ln.quantity),
                "uom": ln.uom or "",
                "unit_price": _num(ln.unit_price),
                "line_total": _num(ln.line_total),
                "gl_code": ln.gl_code or "",
                "tax_code": ln.tax_code or "",
                "department": ln.department or "",
                "project_code": ln.project_code or "",
            })
    return buf.getvalue()


def mark_posted(session: Session, invoice: ApInvoice, result: PostResult) -> None:
    """Record a successful ERP post (or a manual export acknowledgement): stamp the
    ERP's key and flip ``approved → posted``. A failure leaves the invoice approved for
    the retry queue rather than losing it."""
    if not result.ok:
        return
    invoice.erp_doc_entry = result.erp_doc_entry
    if invoice.status == "approved":
        invoice.status = "posted"
    session.flush()


def _num(value) -> str:
    return "" if value is None else format(value, "f")


class ManualCsvConnector:
    """The day-one 'connector': there is no live ERP, so a push is a no-op that returns
    a synthetic receipt keyed to the invoice — the real posting happens when the
    accountant imports :func:`export_ap_csv`. A real connector swaps in here unchanged."""

    def push_ap_invoice(self, invoice: ApInvoice) -> PostResult:
        return PostResult(ok=True, erp_doc_entry=f"CSV-{str(invoice.id)[:8].upper()}")

    def pull_vendors(self) -> list:
        return []

    def pull_gl_accounts(self) -> list:
        return []
