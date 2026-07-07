"""Carbon coverage view (Appendix F-B, FR-S7-lite).

Surfaces the doc-vs-forwarded difference as a FEATURE, not a surprise: per document and
per period, the total **captured** spend (the document's gross) vs the amount actually
**forwarded** to CarbonNext (its carbon-relevant lines only), with a drill-down to the
lines. Because the carbon unit is the line, forwarded ≤ captured is normal — this makes
the remainder (non-carbon spend) visible and reconcilable by reference.

Built from the ``carbon_handoff`` log (e-Claim today; the AP handoff will union in here
once wired, using the same ``doc_no`` / ``doc_gross_total`` fields). Reversals net out
of the forwarded amount, so a backed-out line correctly shows as no longer covered.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import CarbonHandoff

_ZERO = Decimal("0")


@dataclass
class CoverageLine:
    line_id: uuid.UUID
    category_name: str | None
    expense_type: str | None
    vendor: str | None
    amount: Decimal
    direction: str
    carbon_ref: str


@dataclass
class DocumentCoverage:
    period: str
    claim_id: uuid.UUID
    doc_no: str | None
    currency: str | None
    captured: Decimal            # the document's gross total (all lines)
    forwarded: Decimal           # net amount forwarded to CarbonNext (reversals netted)
    line_count: int              # carbon lines forwarded (forward rows)
    lines: list[CoverageLine] = field(default_factory=list)

    @property
    def uncovered(self) -> Decimal:
        return self.captured - self.forwarded

    @property
    def coverage_pct(self) -> int:
        if self.captured <= _ZERO:
            return 0
        return int((self.forwarded / self.captured * 100).to_integral_value())


@dataclass
class PeriodCoverage:
    period: str
    captured: Decimal
    forwarded: Decimal
    doc_count: int
    line_count: int
    documents: list[DocumentCoverage] = field(default_factory=list)

    @property
    def coverage_pct(self) -> int:
        if self.captured <= _ZERO:
            return 0
        return int((self.forwarded / self.captured * 100).to_integral_value())


def _doc_key(row: CarbonHandoff) -> tuple:
    """A document identity for grouping handoff rows: (claim, doc_no) when the row names
    a document, else the individual line — so blank-doc lines never merge into one
    phantom document (mirrors claims._doc_key)."""
    if row.doc_no:
        return (row.claim_id, row.doc_no)
    return (row.claim_id, f"__line__{row.line_id}")


def _period_of(row: CarbonHandoff) -> str:
    """The reporting period a handoff falls in — the release month (deterministic;
    ``doc_date`` is a free-text OCR string and unreliable to bucket by)."""
    return row.created_at.strftime("%Y-%m")


def coverage_report(
    session: Session, client_ids, *, period: str | None = None
) -> list[PeriodCoverage]:
    """Roll the handoff log up into per-period → per-document coverage, newest period
    first. ``period`` ('YYYY-MM') filters to one period. Restricted to the caller's
    visible clients (app-layer narrowing on top of RLS)."""
    if not client_ids:
        return []
    rows = list(session.execute(
        select(CarbonHandoff)
        .where(CarbonHandoff.client_id.in_(client_ids))
        .order_by(CarbonHandoff.created_at, CarbonHandoff.id)
    ).scalars())

    docs: dict[tuple, DocumentCoverage] = {}
    for r in rows:
        p = _period_of(r)
        if period is not None and p != period:
            continue
        key = (p, *_doc_key(r))
        amount = r.amount if r.amount is not None else _ZERO
        # captured = the document's gross; identical on every row of a document. Fall
        # back to the (unsigned) forwarded amount for a pre-F-B row with no gross stored.
        captured = r.doc_gross_total if r.doc_gross_total is not None else abs(amount)
        doc = docs.get(key)
        if doc is None:
            doc = DocumentCoverage(
                period=p, claim_id=r.claim_id, doc_no=r.doc_no, currency=r.currency,
                captured=captured, forwarded=_ZERO, line_count=0,
            )
            docs[key] = doc
        doc.forwarded += amount
        if r.direction == "forward":
            doc.line_count += 1
            doc.lines.append(CoverageLine(
                line_id=r.line_id, category_name=r.category_name,
                expense_type=r.expense_type, vendor=r.vendor, amount=amount,
                direction=r.direction, carbon_ref=r.carbon_ref,
            ))

    periods: dict[str, PeriodCoverage] = {}
    for doc in docs.values():
        pc = periods.get(doc.period)
        if pc is None:
            pc = PeriodCoverage(doc.period, _ZERO, _ZERO, 0, 0)
            periods[doc.period] = pc
        pc.captured += doc.captured
        pc.forwarded += doc.forwarded
        pc.doc_count += 1
        pc.line_count += doc.line_count
        pc.documents.append(doc)

    ordered = sorted(periods.values(), key=lambda p: p.period, reverse=True)
    for pc in ordered:
        pc.documents.sort(key=lambda d: (d.doc_no or "", str(d.claim_id)))
    return ordered
