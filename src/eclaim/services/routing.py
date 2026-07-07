"""Document router (C1): send a classified page to the right queue.

The vision OCR now returns a ``document_type`` + ``type_confidence`` (see
:class:`eclaim.ocr.base.Extraction`). This module turns that classification into a
routing decision, kept PURE (no DB, no I/O) so the capture path, the worker, and the
review UI all share one source of truth and it is trivially testable:

* an **expense_receipt** (something a person paid and claims back) → the e-Claim
  queue, exactly as today;
* a **vendor_invoice** (a bill finance still has to pay) or its **delivery_order** →
  the AP holding queue (``ap_holding``) — captured now, processed when the AP module
  ships; NEVER silently forced into e-Claim;
* anything the model is not confident about (``type_confidence`` below the threshold),
  or an **unknown** type → held for a one-tap manual decision at review
  (``needs_manual``), rather than guessed.

A missing ``type_confidence`` (``None``) means the provider predates the classifier
(or the fake OCR in tests): treated as confident, so the default ``expense_receipt``
path is unchanged and nothing regresses.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ..config import get_settings

# Queue identifiers a page can be routed to.
QUEUE_ECLAIM = "eclaim"          # staff-paid expense → the e-Claim reimbursement flow
QUEUE_AP_HOLDING = "ap_holding"  # vendor bill / DO → the "Vendor bills (coming soon)" queue
QUEUE_PENDING = "pending"        # undecided → awaits a manual route at review


@dataclass(frozen=True)
class Route:
    """The routing decision for one classified page."""

    queue: str          # one of the QUEUE_* constants
    needs_manual: bool  # True → hold for the reviewer's "paid it / vendor bill?" choice


def _threshold() -> Decimal:
    try:
        return Decimal(str(get_settings().route_confidence_threshold))
    except (InvalidOperation, ValueError):
        return Decimal("0.85")


def route(
    document_type: str,
    type_confidence: Decimal | float | None,
    *,
    threshold: Decimal | float | None = None,
) -> Route:
    """Resolve the queue for a classified page. ``threshold`` overrides the configured
    default (Appendix B)."""
    cut = Decimal(str(threshold)) if threshold is not None else _threshold()
    # None confidence = unclassified provider → treat as confident so the default
    # expense_receipt path is unchanged; a real low score below the cut asks the human.
    confident = type_confidence is None or Decimal(str(type_confidence)) >= cut

    if not confident:
        return Route(QUEUE_PENDING, needs_manual=True)
    if document_type == "expense_receipt":
        return Route(QUEUE_ECLAIM, needs_manual=False)
    # AP-side documents captured for reference. Only ``vendor_invoice`` is payable (the
    # holding UI offers "File as AP invoice" for that alone); a delivery_order,
    # quotation and purchase_order are held, labelled, but not billable as-is.
    if document_type in ("vendor_invoice", "delivery_order", "quotation", "purchase_order"):
        return Route(QUEUE_AP_HOLDING, needs_manual=False)
    # "unknown" (even at high confidence in the type "unknown") → ask the human.
    return Route(QUEUE_PENDING, needs_manual=True)


def link_key(vendor: str | None, ref: str | None) -> str | None:
    """A normalized key to link a delivery order to its matching vendor invoice — same
    vendor + same PO/DO reference (C1). Returns ``None`` when either part is missing,
    so an unlinkable page is simply not linked (never mis-linked on a blank key)."""
    v = (vendor or "").strip().casefold()
    r = (ref or "").strip().casefold()
    if not v or not r:
        return None
    return f"{v}|{r}"
