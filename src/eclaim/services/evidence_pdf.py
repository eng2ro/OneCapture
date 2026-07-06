"""Evidence-pack PDF renderer (the presentation half).

Renders an assembled :class:`~eclaim.services.evidence.Evidence` into a PDF with
fpdf2 (pure Python, core Helvetica font — no system libs). The ``generated_at``
stamp is passed in at render time, so the deterministic assembled content stays
separable from the moment of rendering. Smoke-tested only; the data assembly is
where the real test coverage lives.
"""

from __future__ import annotations

import os
from datetime import datetime

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from .evidence import Evidence

_NEXT = {"new_x": XPos.LMARGIN, "new_y": YPos.NEXT}


def _safe(value) -> str:
    """Core-font (latin-1) safe text; chars outside latin-1 become '?'. Keeps the
    renderer from crashing on an accented vendor name etc. (a Unicode TTF font is
    a future nicety)."""
    if value is None:
        return ""
    return str(value).encode("latin-1", "replace").decode("latin-1")


def render(evidence: Evidence, generated_at: datetime) -> bytes:
    """Render the evidence model to PDF bytes."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # -- Header --------------------------------------------------------------
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _safe("Claim Evidence Pack"), **_NEXT)
    pdf.set_font("Helvetica", size=10)
    for line in (
        f"Claim ID : {evidence.claim_id}",
        f"Client   : {evidence.client_name} ({evidence.client_id})",
        f"Status   : {evidence.status}",
        f"Generated: {generated_at.isoformat()}",
    ):
        pdf.cell(0, 6, _safe(line), **_NEXT)

    # -- Source document (embedded receipt) ----------------------------------
    _section(pdf, "Source document")
    if evidence.image_path and os.path.exists(evidence.image_path):
        try:
            pdf.image(evidence.image_path, w=80)
        except Exception:
            pdf.cell(0, 6, _safe("(receipt image could not be embedded)"), **_NEXT)
    else:
        pdf.cell(0, 6, _safe(f"(receipt image unavailable: {evidence.image_path})"), **_NEXT)
    pdf.cell(0, 6, _safe(f"image sha256: {evidence.image_sha256}"), **_NEXT)

    # -- Confirmed fields ----------------------------------------------------
    _section(pdf, "Confirmed fields")
    qty = (
        f"{evidence.quantity} {evidence.unit or ''}".strip()
        if evidence.quantity is not None
        else None
    )
    carbon = (
        None if evidence.carbon_relevant is None
        else ("yes — forwarded to CarbonNext" if evidence.carbon_relevant else "no")
    )
    for label, value in (
        ("Vendor", evidence.vendor),
        ("Doc no", evidence.doc_no),
        ("Doc date", evidence.doc_date),
        ("Currency", evidence.currency),
        ("Total amount", evidence.total_amount),
        ("Quantity", qty),
        ("Category", evidence.category_name),
        ("Carbon relevant", carbon),
    ):
        _kv(pdf, label, value)

    # -- Claimant ------------------------------------------------------------
    _section(pdf, "Claimant")
    if evidence.claimant_name:
        _kv(pdf, "Name", evidence.claimant_name)
        _kv(pdf, "Employee ref", evidence.employee_ref)
        _kv(pdf, "Cost centre", evidence.cost_centre)
    else:
        pdf.cell(0, 6, _safe("(no claimant on record)"), **_NEXT)

    # -- Out-of-pocket attestation (Appendix A) ------------------------------
    _section(pdf, "Out-of-pocket attestation")
    if evidence.attested_by:
        pdf.cell(
            0, 6,
            _safe(
                'Declared: "these out-of-pocket expenses were paid with my own money '
                'and have not been (and will not be) reimbursed elsewhere."'
            ),
            **_NEXT,
        )
        _kv(pdf, "Attested by", evidence.attested_by)
        _kv(
            pdf, "Attested at",
            evidence.attested_at.isoformat() if evidence.attested_at else None,
        )
    else:
        pdf.cell(0, 6, _safe("(no out-of-pocket attestation on record)"), **_NEXT)

    # -- Approval trail ------------------------------------------------------
    _section(pdf, "Approval trail")
    for ev in evidence.trail:
        line = f"{ev.created_at.isoformat()}  {ev.event_type}  by {ev.actor}"
        if ev.reason:
            line += f"  - {ev.reason}"
        pdf.cell(0, 6, _safe(line), **_NEXT)

    # -- Integrity -----------------------------------------------------------
    _section(pdf, "Integrity")
    pdf.set_font("Helvetica", size=8)
    pdf.cell(
        0, 5,
        _safe("Audit chain is hash-linked: each event's prev_hash equals the previous event's hash."),
        **_NEXT,
    )
    for ev in evidence.trail:
        pdf.cell(0, 5, _safe(f"  {ev.event_type}: {ev.hash}"), **_NEXT)
    pdf.set_font("Helvetica", size=10)
    _kv(pdf, "Release batch hash", evidence.batch_hash or "(not released)")
    _kv(pdf, "TSA token", evidence.tsa_token or "(not released)")

    return bytes(pdf.output())


def _section(pdf: FPDF, title: str) -> None:
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 7, _safe(title), **_NEXT)
    pdf.set_font("Helvetica", size=10)


def _kv(pdf: FPDF, label: str, value) -> None:
    pdf.cell(0, 6, _safe(f"{label}: {'' if value is None else value}"), **_NEXT)
