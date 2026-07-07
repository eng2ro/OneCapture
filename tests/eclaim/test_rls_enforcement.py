"""DB-level RLS enforcement on the tenant DATA tables (acceptance criterion).

The app-layer test (``test_tenant_isolation``) proves narrowing on the
firm-gated directory tables. This one proves the *database* itself isolates the
strong DATA tables — claim / release_batch / emission_entry / audit_event — when
queried by the real unprivileged role:

* connect as ``onecapture_app`` (NOSUPERUSER, NOBYPASSRLS) via
  ``APP_TEST_DATABASE_URL`` so RLS actually bites;
* with NO tenant context set, every seeded row is denied (default-deny);
* with ``app.current_firm`` = firm A, firm A sees only its own rows and firm B's
  rows are invisible.

Seeding is done by the owner connection (RLS-bypassing) and COMMITTED so the
separate ``onecapture_app`` connection can see it, then torn down in teardown.
Skips cleanly when no Postgres / no app-role DSN is reachable.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from eclaim.db.models import (
    ApInvoice,
    ApInvoiceLine,
    ApprovalMatrixRule,
    AuditEvent,
    Claim,
    Client,
    DocumentIntake,
    EmissionEntry,
    Firm,
    ReleaseBatch,
    Vendor,
)

# The firm + allowed-client gated tables. (claimant is in the same policy class
# but we seed no claimants.) approval_matrix_rule carries the same firm+client RLS
# policy (migration 0023) and must be proven isolated too (punch-list P7).
# document_intake (migration 0025) uses the worker-inclusive policy — with the worker
# GUC off (as here) it must still default-deny with no context and firm-scope with it.
DATA_TABLES = [
    "claim", "release_batch", "emission_entry", "audit_event", "approval_matrix_rule",
    "document_intake", "vendor", "ap_invoice", "ap_invoice_line",
]


def _seed_firm(session: Session, label: str) -> dict:
    """One self-contained firm: client + claim + batch + entry + audit, all
    stamped with the firm id. Unique-ish keys so a leftover row from a crashed
    prior run can't collide."""
    tag = uuid.uuid4().hex[:8]
    firm = Firm(name=f"RLS Firm {label}")
    session.add(firm)
    session.flush()

    client = Client(firm_id=firm.id, name=f"RLS Client {label}", currency="MYR")
    session.add(client)
    session.flush()

    batch = ReleaseBatch(
        firm_id=firm.id, client_id=client.id, source_type="eclaim",
        created_by="seed", batch_hash=f"hash-{tag}", record_count=1,
        total_tco2e=Decimal("1.000000"),
    )
    session.add(batch)
    session.flush()

    session.add_all([
        Claim(
            firm_id=firm.id, client_id=client.id,
            image_path=f"/x/{tag}.png", image_sha256=tag,
        ),
        EmissionEntry(
            firm_id=firm.id, client_id=client.id, source_type="eclaim",
            source_id=uuid.uuid4(), scope=1, factor_key="fuel_diesel",
            factor_version=1, basis="activity", tco2e=Decimal("1.000000"),
            release_batch_id=batch.id, idempotency_key=f"idem-{tag}",
            carbon_ref=f"CARB-{tag}",
        ),
        AuditEvent(
            firm_id=firm.id, client_id=client.id, entity_type="claim",
            entity_id=uuid.uuid4(), event_type="submitted", actor="seed",
            hash=f"h-{tag}",
        ),
        ApprovalMatrixRule(
            firm_id=firm.id, client_id=client.id, step_order=1,
            approver_role="manager", approvals_required=1, active=True,
        ),
        DocumentIntake(
            firm_id=firm.id, client_id=client.id,
            document_type="vendor_invoice", routed_to="ap_holding",
        ),
    ])
    session.flush()

    # AP domain (C2): vendor → invoice → line, all firm+client scoped.
    vendor = Vendor(firm_id=firm.id, client_id=client.id, name=f"Vendor {tag}")
    session.add(vendor)
    session.flush()
    invoice = ApInvoice(
        firm_id=firm.id, client_id=client.id, vendor_id=vendor.id,
        doc_no=f"INV-{tag}", total_amount=Decimal("100.00"), idempotency_key=f"idem-ap-{tag}",
    )
    session.add(invoice)
    session.flush()
    session.add(ApInvoiceLine(
        firm_id=firm.id, client_id=client.id, ap_invoice_id=invoice.id,
        line_no=1, description="seed", line_total=Decimal("100.00"),
    ))
    session.flush()
    return {"firm": firm.id, "client": client.id}


@pytest.fixture
def two_firms(db_engine):
    """Seed two isolated firms (owner connection, COMMITTED so the app-role
    connection sees them); remove them in teardown."""
    owner = Session(bind=db_engine, future=True, expire_on_commit=False)
    made: dict = {}
    try:
        made = {"a": _seed_firm(owner, "A"), "b": _seed_firm(owner, "B")}
        owner.commit()
        yield made
    finally:
        for entry in made.values():
            fid = entry["firm"]
            for tbl in ["emission_entry", "audit_event", "release_batch",
                        "approval_matrix_rule", "document_intake",
                        "ap_invoice_line", "ap_invoice", "vendor", "claim", "client"]:
                owner.execute(text(f"DELETE FROM {tbl} WHERE firm_id = :f"), {"f": fid})
            owner.execute(text("DELETE FROM firm WHERE id = :f"), {"f": fid})
        owner.commit()
        owner.close()


def _count(conn, table: str, firm_id: uuid.UUID) -> int:
    return conn.execute(
        text(f"SELECT count(*) FROM {table} WHERE firm_id = :f"), {"f": firm_id}
    ).scalar_one()


def test_rls_denies_data_rows_without_context_and_across_firms(two_firms, app_engine):
    a, b = two_firms["a"], two_firms["b"]
    with app_engine.connect() as conn:
        # 1) No tenant context → default-deny: neither firm's rows are visible.
        for tbl in DATA_TABLES:
            assert _count(conn, tbl, a["firm"]) == 0, f"{tbl}: firm A leaked with no context"
            assert _count(conn, tbl, b["firm"]) == 0, f"{tbl}: firm B leaked with no context"

        # 2) Context = firm A. set_config(is_local=false) holds for the session.
        conn.execute(
            text("SELECT set_config('app.current_firm', :v, false)"),
            {"v": str(a["firm"])},
        )
        conn.execute(
            text("SELECT set_config('app.allowed_clients', :v, false)"),
            {"v": str(a["client"])},
        )
        for tbl in DATA_TABLES:
            assert _count(conn, tbl, a["firm"]) == 1, f"{tbl}: firm A cannot see its own row"
            assert _count(conn, tbl, b["firm"]) == 0, f"{tbl}: firm B visible under firm A context"


def test_empty_firm_context_denies_rather_than_errors(app_engine):
    """0003 hardening: a blank ``app.current_firm`` must resolve to NULL → zero
    rows (deny), not raise ``invalid input syntax for type uuid: ""`` from a bare
    cast. Regression guard for the firm-match nullif."""
    with app_engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.current_firm', '', false)"))
        assert conn.execute(text("SELECT count(*) FROM claim")).scalar_one() == 0
