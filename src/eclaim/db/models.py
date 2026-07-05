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


class Event(Base):
    """Optional grouping above a claim — a trip / training / activity that holds
    purpose, attendee count, dates and a BUDGET. One event aggregates across many
    claims AND many people (e.g. several staff each claim part of the same trip).
    Tenant-scoped + RLS like the other e-Claim data tables (migration 0008)."""

    __tablename__ = "event"
    __table_args__ = (Index("ix_event_firm_client", "firm_id", "client_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)

    title: Mapped[str] = mapped_column(String, nullable=False)
    purpose: Mapped[str | None] = mapped_column(String)
    event_type: Mapped[str | None] = mapped_column(String)
    attendee_count: Mapped[int | None] = mapped_column(Integer)
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String)
    department: Mapped[str | None] = mapped_column(String)
    cost_centre: Mapped[str | None] = mapped_column(String)
    project_code: Mapped[str | None] = mapped_column(String)
    budget_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    budget_currency: Mapped[str | None] = mapped_column(String)
    organiser_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Claim(Base):
    __tablename__ = "claim"
    __table_args__ = (
        CheckConstraint("scope IN (1,2,3)", name="ck_claim_scope"),
        CheckConstraint("basis IN ('activity','spend')", name="ck_claim_basis"),
        CheckConstraint(
            "status IN ('submitted','in_review','approved','partially_approved',"
            "'sent_back','rejected','released','exported','paid')",
            name="ck_claim_status",
        ),
        CheckConstraint(
            "claim_type IN ('general','travel','training','client_meeting','other')",
            name="ck_claim_type",
        ),
        # SoD second layer: a firm user who keyed a claim cannot also approve it.
        CheckConstraint(
            "approved_by_user_id IS NULL OR approved_by_user_id <> created_by_user_id",
            name="ck_claim_sod",
        ),
        Index("ix_claim_client_status", "client_id", "status"),
        Index("ix_claim_firm", "firm_id"),
        Index("ix_claim_event", "event_id"),
        # Inbox/export sort key (migration 0016).
        Index("ix_claim_client_created", "client_id", "created_at"),
        # Human-readable reference, unique across the deployment (migration 0016).
        UniqueConstraint("claim_no", name="uq_claim_no"),
        # A claim built from an async ingestion job carries its job id, UNIQUE so a
        # re-claimed/retried job can never create a second claim (migration 0020).
        UniqueConstraint("ingestion_job_id", name="uq_claim_ingestion_job"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)
    # NULL for the inline/interactive path; set to the ingestion_job.id for a claim
    # the background worker built, keying idempotent job completion (B3).
    ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column()

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

    # Header (multi-line redesign, migration 0008). A claim is now a header that
    # owns N ``claim_line`` rows; these carry the per-claim context + rolled-up
    # totals. The legacy per-receipt columns below stay until the Phase-1 cutover
    # (0009) moves the app onto ``claim_line`` and drops them.
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("event.id"))
    # Human-readable claim reference, e.g. 'CLM-2026-000123' (migration 0016).
    claim_no: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    purpose: Mapped[str | None] = mapped_column(String)
    # Claim-level type/purpose (migration 0010). Compulsory, small fixed vocabulary
    # so the approver gets instant context. 'general' is the everyday one-off claim;
    # the others (travel/training/client_meeting/other) describe a multi-day reason
    # and — when the claim has no Event to inherit dates from — require a date range
    # (enforced in ClaimService.start_claim, not the DB).
    claim_type: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'general'")
    )
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    # Document-header grouping fields (migration 0015). ``posting_date`` is the
    # one accounting date for the whole claim (SAP B1-style), distinct from each
    # line's ``posting_date`` override and from a receipt's vendor ``doc_date``.
    # ``remarks`` is free-text commentary (≈ Concur "Comment" — not posted to the
    # ERP), as opposed to ``purpose`` (the "Business Purpose" that does post) and
    # ``approver_note`` (the reviewer's decision note).
    posting_date: Mapped[dt.date | None] = mapped_column(Date)
    remarks: Mapped[str | None] = mapped_column(String)
    # Claim-level cost dimensions (defaults for a standalone claim; a line override
    # wins). Migration 0012.
    department: Mapped[str | None] = mapped_column(String)
    project_code: Mapped[str | None] = mapped_column(String)
    claim_currency: Mapped[str | None] = mapped_column(String)
    period: Mapped[str | None] = mapped_column(String)
    total_claimed: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    total_approved: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    total_reimbursable: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    approver_note: Mapped[str | None] = mapped_column(String)

    # OCR-extracted fields (LEGACY — moving to claim_line; dropped in 0009)
    vendor: Mapped[str | None] = mapped_column(String)
    doc_no: Mapped[str | None] = mapped_column(String)
    doc_date: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str | None] = mapped_column(String)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    expense_type: Mapped[str | None] = mapped_column(String)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    unit: Mapped[str | None] = mapped_column(String)
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))

    # Source image (LEGACY — moved to claim_line; nullable on the header since 0009)
    image_path: Mapped[str | None] = mapped_column(String)
    image_sha256: Mapped[str | None] = mapped_column(String)

    # Classification (LEGACY — moved to claim_line; dropped in a later cleanup)
    scope: Mapped[int | None] = mapped_column(SmallInteger)
    factor_key: Mapped[str | None] = mapped_column(String)
    factor_version: Mapped[int | None] = mapped_column(Integer)
    basis: Mapped[str | None] = mapped_column(String)
    tco2e: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    data_quality: Mapped[str | None] = mapped_column(String)
    # The category this claim was classified under (FR-E6). Nullable: a claim
    # whose expense_type matches no category is 'unmapped' until a reviewer assigns
    # one. Scope is still factor-derived — the category only supplies factor_key.
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("category.id"))

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


class ClaimLine(Base):
    """One receipt/expense line under a :class:`Claim` header (migration 0008).

    This is most of today's per-receipt ``claim`` — OCR fields + carbon
    classification — plus the reimbursement fields (tax, payment method, GL) and a
    per-line review status that drives **partial approval** (a reviewer can approve
    some lines and query/reject others in one action).

    ``carbon_class`` (direct/spend/none) is snapshotted from the category at
    classify time — like ``scope``/``factor_key`` already are — so export and the
    Carbon Next handoff filter on the line, not the live category. There is
    deliberately **no ``tco2e``**: e-Claim forwards the activity data and Carbon
    Next computes the emissions.
    """

    __tablename__ = "claim_line"
    __table_args__ = (
        CheckConstraint("scope IN (1,2,3)", name="ck_claim_line_scope"),
        CheckConstraint("basis IN ('activity','spend')", name="ck_claim_line_basis"),
        CheckConstraint(
            "payment_method IN ('out_of_pocket','corporate_card','company_paid')",
            name="ck_claim_line_payment",
        ),
        CheckConstraint(
            "carbon_class IN ('direct','spend','none')", name="ck_claim_line_carbon_class"
        ),
        CheckConstraint(
            "line_status IN ('pending','approved','queried','rejected')",
            name="ck_claim_line_status",
        ),
        Index("ix_claim_line_claim", "claim_id"),
        Index("ix_claim_line_firm_client", "firm_id", "client_id"),
        # One line number per claim (migration 0016) — guards next_line_no's race.
        UniqueConstraint("claim_id", "line_no", name="uq_claim_line_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claim.id"), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    # OCR-extracted (moved from claim)
    vendor: Mapped[str | None] = mapped_column(String)
    doc_no: Mapped[str | None] = mapped_column(String)
    doc_date: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str | None] = mapped_column(String)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    expense_type: Mapped[str | None] = mapped_column(String)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    unit: Mapped[str | None] = mapped_column(String)
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    # Nullable since 0014: a mileage line has a route, not a receipt image.
    image_path: Mapped[str | None] = mapped_column(String)
    image_sha256: Mapped[str | None] = mapped_column(String)
    # Constituent page images when a line was merged (or split back) — an ordered
    # list [{sha, path}, …] (migration 0017). NULL = an ordinary single-image line
    # (its ``image_path`` is the one image). Lets a merged line remember its parts so
    # it can be split again; ``image_path`` holds the stitched composite for display.
    pages: Mapped[list | None] = mapped_column(JSONB)
    # Per-field OCR bounding boxes for the receipt viewer overlay (migration 0013):
    # { field_name: [x, y, w, h] } normalized 0..1, origin top-left.
    ocr_boxes: Mapped[dict | None] = mapped_column(JSONB)

    # Reimbursement + accounting-coding fields (a claim line is a source document).
    business_reason: Mapped[str | None] = mapped_column(String)
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    tax_code: Mapped[str | None] = mapped_column(String)
    tax_inclusive: Mapped[bool | None] = mapped_column()
    net_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    base_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    payment_method: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'out_of_pocket'")
    )
    reimbursable: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    gl_code: Mapped[str | None] = mapped_column(String)
    cost_centre_override: Mapped[str | None] = mapped_column(String)
    # Added 0012: posting date (vs invoice doc_date), vendor tax-reg, cost dimensions.
    posting_date: Mapped[dt.date | None] = mapped_column(Date)
    supplier_tax_id: Mapped[str | None] = mapped_column(String)
    department: Mapped[str | None] = mapped_column(String)
    project_code: Mapped[str | None] = mapped_column(String)
    attendees: Mapped[list | None] = mapped_column(JSONB)
    mileage: Mapped[dict | None] = mapped_column(JSONB)
    per_diem: Mapped[dict | None] = mapped_column(JSONB)
    policy_result: Mapped[str | None] = mapped_column(String)

    # Carbon classification (moved from claim; NO tco2e — Carbon Next computes it)
    scope: Mapped[int | None] = mapped_column(SmallInteger)
    factor_key: Mapped[str | None] = mapped_column(String)
    factor_version: Mapped[int | None] = mapped_column(Integer)
    basis: Mapped[str | None] = mapped_column(String)
    data_quality: Mapped[str | None] = mapped_column(String)
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("category.id"))
    carbon_class: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'none'")
    )
    # Snapshot of the category's carbon_relevant at capture (migration 0011): is
    # this line forwarded to CarbonNext on release? e-Claim no longer fills
    # scope/factor_key/factor_version/basis/data_quality above — they are vestigial.
    carbon_relevant: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true")
    )

    # Per-line review state (partial approval)
    line_status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'pending'")
    )
    line_reason: Mapped[str | None] = mapped_column(String)

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
    # Nullable since 0009: e-Claim stops computing tCO2e (Carbon Next does), so an
    # e-Claim batch carries NULL here. ERP Sync still writes real tonnage.
    total_tco2e: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
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


class Category(Base):
    """Per-client expense_type → factor mapping master (FR-E6).

    Maps an OCR ``expense_type`` to an emission ``factor_key`` (NULL = spend-based
    by intent) plus a GL export code and a default limit. It deliberately has NO
    ``scope`` column — scope stays derived from the resolved factor in
    ``services/classify.py``, so the two can never drift. Tenant-scoped + RLS like
    the other e-Claim data tables (see migration 0006)."""

    __tablename__ = "category"
    __table_args__ = (
        # Name is the human-unique key per client. expense_type is intentionally
        # NOT unique: many staff categories (meals, taxi, parking...) legitimately
        # share expense_type='other', and a claim now picks its category directly
        # via category_id rather than by expense_type. (migration 0007)
        UniqueConstraint("client_id", "name", name="uq_category_client_name"),
        Index("ix_category_firm_client", "firm_id", "client_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    expense_type: Mapped[str] = mapped_column(String, nullable=False)  # the OCR map key
    factor_key: Mapped[str | None] = mapped_column(String)  # EF ref; NULL = spend-based
    # Curated carbon class (migration 0008): 'direct' = real activity factor,
    # 'spend' = spend-based estimate, 'none' = non-carbon (excluded from the Carbon
    # Next handoff). Snapshotted onto each claim_line at classify time.
    carbon_class: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'none'")
    )
    # The single carbon field e-Claim keeps (migration 0011): does this category's
    # spend get forwarded to CarbonNext? e-Claim does NO carbon maths — CarbonNext
    # owns scope/factor/tonnage. ``carbon_class``/``factor_key`` above are now
    # vestigial (kept for back-compat; the app reads ``carbon_relevant``).
    carbon_relevant: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true")
    )
    gl_export_code: Mapped[str | None] = mapped_column(String)
    default_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


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
    # Nullable since 0009: e-Claim forwards activity data and Carbon Next computes
    # the tonnage, so an e-Claim entry carries NULL. ERP Sync still writes tonnage.
    tco2e: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    # The carbon class of the source line (e-Claim, 0009). NULL for ERP Sync rows.
    carbon_class: Mapped[str | None] = mapped_column(String)
    release_batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("release_batch.id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    carbon_ref: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CarbonHandoff(Base):
    """One forwarded e-Claim line -> CarbonNext (migration 0011).

    e-Claim does NO carbon maths: on release it forwards the RAW expense data of
    each carbon-relevant approved line (category, amount, currency, quantity, unit,
    vendor, date, cost centre). CarbonNext maps that to scope/factor and computes
    the tonnage. This is e-Claim's own handoff log — it deliberately does NOT use
    the shared ``emission_entry`` ledger (that needs scope/factor e-Claim no longer
    resolves, and stays exclusively for ERP Sync).

    ``direction`` = 'forward' (normal) or 'reversal' (a correction telling Carbon
    Next to back out an earlier forward). Tenant-scoped + RLS like the other tables.
    """

    __tablename__ = "carbon_handoff"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('forward','reversal')", name="ck_carbon_handoff_direction"
        ),
        UniqueConstraint("idempotency_key", name="uq_carbon_handoff_idem"),
        Index("ix_carbon_handoff_client", "client_id"),
        Index("ix_carbon_handoff_firm", "firm_id"),
        Index("ix_carbon_handoff_batch", "release_batch_id"),
        Index("ix_carbon_handoff_line", "line_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claim.id"), nullable=False)
    line_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claim_line.id"), nullable=False)
    release_batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("release_batch.id"), nullable=False
    )

    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("category.id"))
    category_name: Mapped[str | None] = mapped_column(String)
    expense_type: Mapped[str | None] = mapped_column(String)
    vendor: Mapped[str | None] = mapped_column(String)
    doc_date: Mapped[str | None] = mapped_column(String)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    unit: Mapped[str | None] = mapped_column(String)
    cost_centre: Mapped[str | None] = mapped_column(String)

    direction: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'forward'")
    )
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    carbon_ref: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ErpsyncEntry(Base):
    """ERP Sync per-line staging row — the rich record for EVERY imported AP
    line, carrying a review ``status``.

    This is to ERP Sync what ``claim`` is to e-Claim: the reviewable staging
    table that a later release projects into the shared ``emission_entry``
    ledger. ALL accepted lines land here — clean (mapped + measured), ``held``
    (cross-channel dedup), and ``flagged`` (unmapped / spend-based / DQ) —
    distinguished by ``status``, not by a separate table. Malformed (REJECTED)
    rows never reach this table; they stay in the import validation report only.

    Columns mirror :class:`erpsync.domain.models.EmissionEntry` (the pipeline's
    output) plus tenancy. ``scope`` is the ERP Sync string scope
    (``scope_1``/``scope_2``/``scope_3_*``), not the e-Claim smallint. Tenant
    isolation matches the other data tables: firm + allowed-client RLS with the
    0003-hardened firm cast.
    """

    __tablename__ = "erpsync_entry"
    __table_args__ = (
        CheckConstraint(
            "status IN ('clean','held','flagged','approved','dismissed','released')",
            name="ck_erpsync_entry_status",
        ),
        # SoD second layer: the maker (editor) cannot also be the checker
        # (reviewer) — mirrors ck_claim_sod. Dynamic guard runs at the service.
        CheckConstraint(
            "reviewed_by_user_id IS NULL OR reviewed_by_user_id <> edited_by_user_id",
            name="ck_erpsync_entry_sod",
        ),
        CheckConstraint(
            "scope IN ('scope_1','scope_2','scope_3_4','scope_3_11','scope_3_other')",
            name="ck_erpsync_entry_scope",
        ),
        CheckConstraint("basis IN ('activity','spend')", name="ck_erpsync_entry_basis"),
        CheckConstraint(
            "data_quality IN ('measured','estimated','flagged')",
            name="ck_erpsync_entry_dq",
        ),
        # Idempotency grain: one staged row per (client, DocEntry, LineNum).
        UniqueConstraint(
            "client_id", "doc_entry", "line_num", name="uq_erpsync_entry_line"
        ),
        Index("ix_erpsync_entry_firm", "firm_id"),
        Index("ix_erpsync_entry_client_status", "client_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)

    # Source line identity — the (client_id, doc_entry, line_num) idempotency grain.
    doc_entry: Mapped[str] = mapped_column(String, nullable=False)
    line_num: Mapped[int] = mapped_column(Integer, nullable=False)
    doc_number: Mapped[str | None] = mapped_column(String)

    # Carbon classification result (the EmissionEntry projection).
    category: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    basis: Mapped[str] = mapped_column(String, nullable=False)
    data_quality: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    uom: Mapped[str | None] = mapped_column(String)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    factor_ref: Mapped[str] = mapped_column(String, nullable=False, server_default=text("''"))
    factor_value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    factor_version: Mapped[str] = mapped_column(String, nullable=False)
    rule_id: Mapped[str] = mapped_column(String, nullable=False, server_default=text("''"))
    rule_version: Mapped[str] = mapped_column(String, nullable=False)
    tco2e: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    source_hash: Mapped[str] = mapped_column(String, nullable=False)
    notes: Mapped[list | None] = mapped_column(JSONB)

    # Review state. ``status`` carries the lifecycle (clean/held/flagged →
    # approved/dismissed → released); the SoD actors are null until a reviewer
    # touches the row (auto-clean rows release without ever being reviewed).
    status: Mapped[str] = mapped_column(String, nullable=False)
    edited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(String)
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


class IngestionJob(Base):
    """Durable queue row for asynchronous capture of a large upload.

    ``/capture`` stages the raw files and inserts one of these; the in-process
    worker claims it (``FOR UPDATE SKIP LOCKED``), builds the claim in the
    background, and updates ``done_units``/``total_units`` for the progress page.
    ``payload`` holds everything the worker needs: the principal snapshot
    (firm/client/user/allowed clients), the header fields, the client-side items,
    the mileage specs, and the staged-file manifest. See migration 0018 for the
    RLS policy that lets the worker claim across tenants via ``app.worker``."""

    __tablename__ = "ingestion_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','done','failed')",
            name="ck_ingestion_job_status",
        ),
        Index("ix_ingestion_job_queue", "status", "created_at"),
        Index("ix_ingestion_job_client", "client_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    firm_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("firm.id"), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("client.id"), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    claim_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("claim.id"))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'queued'"))
    total_units: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    done_units: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
