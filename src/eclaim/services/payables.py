"""Payables overview: the two different pots of money OUT — reimburse staff vs pay
vendors — surfaced together with a total for each.

A staff **reimbursement** (an e-Claim, paid back to the employee) and a **vendor bill**
(an AP invoice, paid to the supplier) are genuinely different workflows — different
approver, separation of duties, payment run and posting — so they stay in separate
modules. This read-only service just aggregates the two "still to pay" totals + lists
for a single overview page, so finance can see cash out at a glance without conflating
them into one claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ApInvoice, Claim, Vendor
from ..repositories import ClaimRepository
from . import ap as ap_service

_ZERO = Decimal("0")

# Reimbursements OWED to staff: approved (or further along) but not yet paid, and with
# reimbursable out-of-pocket spend on them. 'paid'/'rejected' are excluded (done / won't
# pay); pre-approval statuses aren't yet a committed payable.
REIMBURSE_STATUSES = ("approved", "partially_approved", "released", "exported")
# Vendor bills OWED: approved or posted to the ERP but not yet paid.
PAY_STATUSES = ("approved", "posted")


@dataclass
class Payables:
    reimburse_total: Decimal
    reimburse_count: int
    claims: list = field(default_factory=list)      # Claim rows to reimburse
    pay_total: Decimal = _ZERO
    pay_count: int = 0
    invoices: list = field(default_factory=list)     # ApInvoice rows to pay
    vendors: dict = field(default_factory=dict)      # vendor_id -> Vendor (for the AP list)


def payables(session: Session, client_ids) -> Payables:
    """The reimburse-staff and pay-vendors totals + lists for the caller's clients."""
    if not client_ids:
        return Payables(_ZERO, 0)

    claims_all = ClaimRepository(session).list_for_clients(client_ids, REIMBURSE_STATUSES)
    claims = [c for c in claims_all if (c.total_reimbursable or _ZERO) > _ZERO]
    reimburse_total = sum((c.total_reimbursable or _ZERO for c in claims), _ZERO)

    invoices = [
        inv for inv in ap_service.list_invoices(session, client_ids)
        if inv.status in PAY_STATUSES
    ]
    pay_total = sum((inv.total_amount or _ZERO for inv in invoices), _ZERO)
    vendors: dict = {}
    if invoices:
        vids = {inv.vendor_id for inv in invoices}
        vendors = {
            v.id: v for v in session.execute(
                select(Vendor).where(Vendor.id.in_(vids))
            ).scalars()
        }

    return Payables(
        reimburse_total=reimburse_total, reimburse_count=len(claims), claims=claims,
        pay_total=pay_total, pay_count=len(invoices), invoices=invoices, vendors=vendors,
    )
