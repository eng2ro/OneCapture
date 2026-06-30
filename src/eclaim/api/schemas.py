"""API response/request models (decoupled from the ORM)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from ..db.models import AuditEvent, CarbonHandoff, Claim, Client, ReleaseBatch


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: str
    currency: str

    @classmethod
    def of(cls, client: Client) -> "ClientOut":
        return cls.model_validate(client)


class ClaimLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_no: int
    vendor: str | None
    doc_no: str | None
    doc_date: str | None
    currency: str | None
    total_amount: Decimal | None
    expense_type: str | None
    quantity: Decimal | None
    unit: str | None
    payment_method: str
    reimbursable: bool
    business_reason: str | None
    # Accounting coding (a postable source document).
    gl_code: str | None
    cost_centre_override: str | None
    department: str | None
    project_code: str | None
    posting_date: dt.date | None
    supplier_tax_id: str | None
    tax_amount: Decimal | None
    tax_code: str | None
    tax_inclusive: bool | None
    net_amount: Decimal | None
    fx_rate: Decimal | None
    base_amount: Decimal | None
    # e-Claim does no carbon maths — just whether this line forwards to CarbonNext.
    carbon_relevant: bool
    category_id: uuid.UUID | None
    line_status: str
    line_reason: str | None
    image_sha256: str | None   # None for a mileage line (route, not receipt)


class ClaimOut(BaseModel):
    """A claim header + its lines. The per-receipt fields are flattened from the
    FIRST line for the single-receipt API path / back-compat; the full set is in
    ``lines``. e-Claim does no carbon maths — only a per-line ``carbon_relevant``
    flag (what forwards to CarbonNext)."""

    id: uuid.UUID
    claim_no: str | None
    status: str
    source_channel: str
    title: str | None
    purpose: str | None
    remarks: str | None
    event_id: uuid.UUID | None
    claim_currency: str | None
    total_claimed: Decimal | None
    total_approved: Decimal | None
    total_reimbursable: Decimal | None
    # Flattened first line (single-receipt convenience)
    vendor: str | None
    doc_no: str | None
    doc_date: str | None
    currency: str | None
    total_amount: Decimal | None
    expense_type: str | None
    quantity: Decimal | None
    unit: str | None
    ocr_confidence: Decimal | None
    # Flattened coding (single-receipt convenience; full set in ``lines``).
    gl_code: str | None
    cost_centre_override: str | None
    tax_amount: Decimal | None
    net_amount: Decimal | None
    fx_rate: Decimal | None
    base_amount: Decimal | None
    posting_date: dt.date | None
    department: str | None
    project_code: str | None
    carbon_relevant: bool | None
    category_id: uuid.UUID | None
    image_sha256: str | None
    lines: list[ClaimLineOut]

    @classmethod
    def of(cls, claim: Claim, lines=None) -> "ClaimOut":
        lines = list(lines or [])
        first = lines[0] if lines else None

        def g(attr):
            return getattr(first, attr, None)

        return cls(
            id=claim.id,
            claim_no=claim.claim_no,
            status=claim.status,
            source_channel=claim.source_channel,
            title=claim.title,
            purpose=claim.purpose,
            remarks=claim.remarks,
            event_id=claim.event_id,
            claim_currency=claim.claim_currency,
            total_claimed=claim.total_claimed,
            total_approved=claim.total_approved,
            total_reimbursable=claim.total_reimbursable,
            vendor=g("vendor"),
            doc_no=g("doc_no"),
            doc_date=g("doc_date"),
            currency=g("currency"),
            total_amount=g("total_amount"),
            expense_type=g("expense_type"),
            quantity=g("quantity"),
            unit=g("unit"),
            ocr_confidence=g("ocr_confidence"),
            gl_code=g("gl_code"),
            cost_centre_override=g("cost_centre_override"),
            tax_amount=g("tax_amount"),
            net_amount=g("net_amount"),
            fx_rate=g("fx_rate"),
            base_amount=g("base_amount"),
            posting_date=g("posting_date"),
            department=g("department"),
            project_code=g("project_code"),
            carbon_relevant=g("carbon_relevant"),
            category_id=g("category_id"),
            image_sha256=g("image_sha256"),
            lines=[ClaimLineOut.model_validate(ln) for ln in lines],
        )


class ClaimEdit(BaseModel):
    vendor: str | None = None
    doc_no: str | None = None
    doc_date: str | None = None
    currency: str | None = None
    total_amount: Decimal | None = None
    expense_type: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    business_reason: str | None = None
    payment_method: str | None = None
    # Accounting coding a reviewer sets before the claim can post.
    gl_code: str | None = None
    cost_centre_override: str | None = None
    department: str | None = None
    project_code: str | None = None
    posting_date: dt.date | None = None
    supplier_tax_id: str | None = None
    tax_amount: Decimal | None = None
    tax_code: str | None = None
    tax_inclusive: bool | None = None
    fx_rate: Decimal | None = None
    # Reviewer assigns a category (drives carbon_relevant + GL default).
    category_id: uuid.UUID | None = None


class ClaimDecision(BaseModel):
    """Body for send-back / reject — the reviewer's reason (kept in the audit
    trail, not a claim column)."""

    reason: str | None = None


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    batch_hash: str
    tsa_token: str | None
    record_count: int
    total_tco2e: Decimal | None  # NULL for e-Claim (Carbon Next computes tonnage)
    status: str

    @classmethod
    def of(cls, batch: ReleaseBatch) -> "BatchOut":
        return cls.model_validate(batch)


class EntryOut(BaseModel):
    """One RAW line forwarded to CarbonNext (a ``carbon_handoff`` row). NO
    scope/factor/tCO2e — e-Claim does no carbon maths; CarbonNext maps the category
    + amount/quantity to emissions. ``direction`` is 'forward' or 'reversal'."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    claim_id: uuid.UUID
    line_id: uuid.UUID
    category_name: str | None
    expense_type: str | None
    vendor: str | None
    doc_date: str | None
    amount: Decimal | None
    currency: str | None
    quantity: Decimal | None
    unit: str | None
    direction: str
    release_batch_id: uuid.UUID
    carbon_ref: str

    @classmethod
    def of(cls, handoff: CarbonHandoff) -> "EntryOut":
        return cls.model_validate(handoff)


class LedgerOut(BaseModel):
    """CarbonNext handoff log — counts of lines FORWARDED vs REVERSED (no tonnage,
    no scope split: e-Claim forwards raw data and CarbonNext computes emissions)."""

    entries: list[EntryOut]
    forwarded: int
    reversed: int
    total_records: int


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    actor: str
    prev_hash: str | None
    hash: str
    detail: dict | None
    created_at: dt.datetime

    @classmethod
    def of(cls, event: AuditEvent) -> "AuditEventOut":
        return cls.model_validate(event)
