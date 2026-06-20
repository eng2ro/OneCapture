"""SQLAlchemy 2.0 models for the OneCapture database.

Mirrors the spec DDL (eclaim_postgres_spec.md §3). Several tables are *shared*
with ERP Sync and discriminate origin via ``source_type``:

* ``client``, ``emission_factor``, ``release_batch``, ``emission_entry``,
  ``audit_event`` — shared.
* ``claim`` — e-Claim only.

Money/emissions are ``Numeric`` (never float); timestamps are ``timestamptz``;
PKs are UUIDs minted by Postgres ``gen_random_uuid()`` (needs ``pgcrypto``).

The authoritative schema is the Alembic migration; these models must stay in
step with it (the test suite builds the DB from the migration, not from
``create_all``, so a drift shows up as a failing test).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_UUID_DEFAULT = text("gen_random_uuid()")


class Base(DeclarativeBase):
    pass


class Firm(Base):
    """Accountant practice — the top of the tenancy tree (owns firm-wide users)."""

    __tablename__ = "firm"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Client(Base):
    """A company whose claims/invoices are processed. Belongs to one firm.

    This is the spine tenant: the pre-existing e-Claim ``client`` table extended
    in-place with ``firm_id`` and the multi-tenant/CarbonNext-mapping columns.
    """

    __tablename__ = "client"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    ssm_no: Mapped[str | None] = mapped_column(String, unique=True)
    currency: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'MYR'"))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    # jsonb feature flags: {"eclaim": true, "erpsync": false, "ap": false, "ar": false}
    modules: Mapped[dict | None] = mapped_column(JSONB)
    whatsapp_number: Mapped[str | None] = mapped_column(String)
    # The CarbonNext company this client maps to (CarbonNext's id type). Nullable
    # until mapped; unique so one OneCapture client ↔ one CarbonNext company.
    carbonnext_company_id: Mapped[str | None] = mapped_column(String, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmissionFactor(Base):
    __tablename__ = "emission_factor"
    __table_args__ = (
        CheckConstraint("scope IN (1,2,3)", name="ck_factor_scope"),
        UniqueConstraint("factor_key", "version", name="uq_factor_key_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    factor_key: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False)
    factor_kg_per_unit: Mapped[Decimal] = mapped_column(Numeric(12, 5), nullable=False)
    source: Mapped[str | None] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    effective_from: Mapped[dt.date] = mapped_column(
        Date, nullable=False, server_default=func.current_date()
    )
    active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))


class Claim(Base):
    __tablename__ = "claim"
    __table_args__ = (
        CheckConstraint("scope IN (1,2,3)", name="ck_claim_scope"),
        CheckConstraint("basis IN ('activity','spend')", name="ck_claim_basis"),
        CheckConstraint(
            "status IN ('submitted','in_review','approved','released','rejected')",
            name="ck_claim_status",
        ),
        # SoD second layer: a firm user who keyed a claim cannot also approve it.
        CheckConstraint(
            "approved_by_user_id IS NULL OR approved_by_user_id <> created_by_user_id",
            name="ck_claim_sod",
        ),
        Index("ix_claim_client_status", "client_id", "status"),
        Index("ix_claim_firm", "firm_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)

    # Separation-of-duties actors (nullable: pre-spine rows + unauthenticated
    # paths leave them null, which the SoD CHECK permits).
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    submitted_by_claimant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("claimant.id")
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    source_channel: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'upload'")
    )
    claimant_ref: Mapped[str | None] = mapped_column(String)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # OCR-extracted fields
    vendor: Mapped[str | None] = mapped_column(String)
    doc_no: Mapped[str | None] = mapped_column(String)
    doc_date: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str | None] = mapped_column(String)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    expense_type: Mapped[str | None] = mapped_column(String)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    unit: Mapped[str | None] = mapped_column(String)
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))

    # Source image (local disk now; object-storage key later)
    image_path: Mapped[str] = mapped_column(String, nullable=False)
    image_sha256: Mapped[str] = mapped_column(String, nullable=False)

    # Classification (computed by the carbon module)
    scope: Mapped[int | None] = mapped_column(SmallInteger)
    factor_key: Mapped[str | None] = mapped_column(String)
    factor_version: Mapped[int | None] = mapped_column(Integer)
    basis: Mapped[str | None] = mapped_column(String)
    tco2e: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    data_quality: Mapped[str | None] = mapped_column(String)

    # Lifecycle
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'in_review'")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ReleaseBatch(Base):
    __tablename__ = "release_batch"
    __table_args__ = (
        CheckConstraint("source_type IN ('eclaim','erpsync')", name="ck_batch_source"),
        Index("ix_batch_firm", "firm_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    batch_hash: Mapped[str] = mapped_column(String, nullable=False)
    tsa_token: Mapped[str | None] = mapped_column(String)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_tco2e: Mapped[Decimal] = mapped_column(Numeric(16, 6), nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'released'")
    )


# Firm-user roles. Partner/Manager = firm scope (all clients); Approver/Viewer =
# client scope (only granted clients). 'Submitter' is virtual (claimant, no account).
BASE_ROLES = ("partner", "manager", "approver", "viewer")
FIRM_SCOPED_ROLES = frozenset({"partner", "manager"})
CLIENT_SCOPED_ROLES = frozenset({"approver", "viewer"})


class AppUser(Base):
    """A firm user (accountant practice staff). Authenticates via AuthProvider."""

    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint(
            "base_role IN ('partner','manager','approver','viewer')",
            name="ck_user_base_role",
        ),
        UniqueConstraint("firm_id", "email", name="uq_user_firm_email"),
        Index("ix_user_firm", "firm_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    entra_object_id: Mapped[str | None] = mapped_column(String)
    email: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    base_role: Mapped[str] = mapped_column(String, nullable=False)
    authority_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserClientGrant(Base):
    """Grants a client-scoped user access to one client. firm_id is denormalised
    here so RLS can scope the grant table by firm during principal bootstrap."""

    __tablename__ = "user_client_grant"
    __table_args__ = (
        UniqueConstraint("user_id", "client_id", name="uq_grant_user_client"),
        Index("ix_grant_firm", "firm_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)


class Claimant(Base):
    """A submitter known by channel binding (WhatsApp phone / email) — no
    credentials, never authenticates. Identity resolves via channel value."""

    __tablename__ = "claimant"
    __table_args__ = (
        UniqueConstraint("client_id", "phone", name="uq_claimant_client_phone"),
        Index("ix_claimant_firm_client", "firm_id", "client_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    employee_ref: Mapped[str | None] = mapped_column(String)
    cost_centre: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))


class EmissionEntry(Base):
    __tablename__ = "emission_entry"
    __table_args__ = (
        CheckConstraint("scope IN (1,2,3)", name="ck_entry_scope"),
        CheckConstraint("basis IN ('activity','spend')", name="ck_entry_basis"),
        CheckConstraint("source_type IN ('eclaim','erpsync')", name="ck_entry_source"),
        UniqueConstraint("idempotency_key", name="uq_entry_idempotency"),
        Index("ix_entry_client_batch", "client_id", "release_batch_id"),
        Index("ix_entry_firm", "firm_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    scope: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    factor_key: Mapped[str] = mapped_column(String, nullable=False)
    factor_version: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    unit: Mapped[str | None] = mapped_column(String)
    basis: Mapped[str] = mapped_column(String, nullable=False)
    tco2e: Mapped[Decimal] = mapped_column(Numeric(16, 6), nullable=False)
    release_batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("release_batch.id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    carbon_ref: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_firm", "firm_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    prev_hash: Mapped[str | None] = mapped_column(String)
    hash: Mapped[str] = mapped_column(String, nullable=False)
    ip: Mapped[str | None] = mapped_column(String)
    device: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
