"""API response/request models (decoupled from the ORM)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from ..db.models import AuditEvent, Claim, Client, EmissionEntry, ReleaseBatch


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: str
    currency: str

    @classmethod
    def of(cls, client: Client) -> "ClientOut":
        return cls.model_validate(client)


class ClaimOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    source_channel: str
    vendor: str | None
    doc_no: str | None
    doc_date: str | None
    currency: str | None
    total_amount: Decimal | None
    expense_type: str | None
    quantity: Decimal | None
    unit: str | None
    ocr_confidence: Decimal | None
    scope: int | None
    factor_key: str | None
    factor_version: int | None
    basis: str | None
    tco2e: Decimal | None
    data_quality: str | None
    category_id: uuid.UUID | None
    image_sha256: str

    @classmethod
    def of(cls, claim: Claim) -> "ClaimOut":
        return cls.model_validate(claim)


class ClaimEdit(BaseModel):
    vendor: str | None = None
    doc_no: str | None = None
    doc_date: str | None = None
    currency: str | None = None
    total_amount: Decimal | None = None
    expense_type: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    # Reviewer assigns a category to (re)classify the claim through its factor_key.
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
    total_tco2e: Decimal
    status: str

    @classmethod
    def of(cls, batch: ReleaseBatch) -> "BatchOut":
        return cls.model_validate(batch)


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    scope: int
    factor_key: str
    factor_version: int
    quantity: Decimal | None
    unit: str | None
    basis: str
    tco2e: Decimal
    release_batch_id: uuid.UUID
    carbon_ref: str

    @classmethod
    def of(cls, entry: EmissionEntry) -> "EntryOut":
        return cls.model_validate(entry)


class LedgerOut(BaseModel):
    entries: list[EntryOut]
    scope_1: Decimal
    scope_2: Decimal
    scope_3: Decimal
    total_tco2e: Decimal


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
