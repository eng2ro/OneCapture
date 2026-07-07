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
# pay); pre-approval statuses aren't yet a committed payable. Only released/exported
# claims are PAYABLE (mark_paid requires release — the attestation gate and CarbonNext
# handoff live there); approved ones are listed with a "release first" affordance.
REIMBURSE_STATUSES = ("approved", "partially_approved", "released", "exported")
PAYABLE_CLAIM_STATUSES = ("released", "exported")
# Vendor bills OWED: approved or posted to the ERP but not yet paid.
PAY_STATUSES = ("approved", "posted")


def _fmt_ccy(ccy: str) -> str:
    return "RM" if (ccy or "MYR").upper() in ("MYR", "RM") else (ccy or "MYR").upper()


def _display(by_ccy: dict[str, Decimal]) -> str:
    """Render per-currency totals honestly — 'RM 1,200.00 + USD 300.00' — instead of
    summing mixed currencies into one number labelled RM."""
    if not by_ccy:
        return "RM 0.00"
    return " + ".join(f"{_fmt_ccy(c)} {amount:,.2f}" for c, amount in sorted(by_ccy.items()))


def _merge(*dicts: dict[str, Decimal]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for d in dicts:
        for c, amount in d.items():
            out[c] = out.get(c, _ZERO) + amount
    return out


@dataclass
class Payables:
    reimburse_total: Decimal
    reimburse_count: int
    claims: list = field(default_factory=list)      # Claim rows to reimburse
    pay_total: Decimal = _ZERO
    pay_count: int = 0
    invoices: list = field(default_factory=list)     # ApInvoice rows to pay
    vendors: dict = field(default_factory=dict)      # vendor_id -> Vendor (for the AP list)
    # Per-currency views (a USD invoice must not be silently summed into an RM figure).
    reimburse_by_ccy: dict = field(default_factory=dict)
    pay_by_ccy: dict = field(default_factory=dict)
    reimburse_display: str = "RM 0.00"
    pay_display: str = "RM 0.00"
    combined_display: str = "RM 0.00"


def payables(session: Session, client_ids) -> Payables:
    """The reimburse-staff and pay-vendors totals + lists for the caller's clients."""
    if not client_ids:
        return Payables(_ZERO, 0)

    claims_all = ClaimRepository(session).list_for_clients(client_ids, REIMBURSE_STATUSES)
    claims = [c for c in claims_all if (c.total_reimbursable or _ZERO) > _ZERO]
    reimburse_total = sum((c.total_reimbursable or _ZERO for c in claims), _ZERO)
    reimburse_by_ccy: dict[str, Decimal] = {}
    for c in claims:
        ccy = (c.claim_currency or "MYR").upper()
        reimburse_by_ccy[ccy] = reimburse_by_ccy.get(ccy, _ZERO) + (c.total_reimbursable or _ZERO)

    invoices = [
        inv for inv in ap_service.list_invoices(session, client_ids)
        if inv.status in PAY_STATUSES
    ]
    pay_total = sum((inv.total_amount or _ZERO for inv in invoices), _ZERO)
    pay_by_ccy: dict[str, Decimal] = {}
    for inv in invoices:
        ccy = (inv.currency or "MYR").upper()
        pay_by_ccy[ccy] = pay_by_ccy.get(ccy, _ZERO) + (inv.total_amount or _ZERO)
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
        reimburse_by_ccy=reimburse_by_ccy, pay_by_ccy=pay_by_ccy,
        reimburse_display=_display(reimburse_by_ccy),
        pay_display=_display(pay_by_ccy),
        combined_display=_display(_merge(reimburse_by_ccy, pay_by_ccy)),
    )
