"""Seed demo reference data: the emission-factor library + one client + a login
user so the web UI is reachable out of the box.

Run after ``alembic upgrade head``::

    python scripts/seed.py

Idempotent: re-running won't duplicate the demo client, factors, or user. Factor
values are demo placeholders (decision D14) until the carbon lead sets the real
set.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from eclaim.config import get_settings
from eclaim.db.models import AppUser, Category, Client, EmissionFactor

# factor_key, label, scope, unit, kgCO2e/unit  (demo placeholders, D14)
DEMO_FACTORS = [
    ("fuel_diesel", "Diesel (fleet)", 1, "L", Decimal("2.68000")),
    ("fuel_petrol", "Petrol (fleet)", 1, "L", Decimal("2.31000")),
    ("electricity", "Grid electricity (MY)", 2, "kWh", Decimal("0.58500")),
    ("natural_gas", "Natural gas", 1, "m3", Decimal("2.03000")),
    ("air_travel", "Air travel", 3, "km", Decimal("0.18000")),
    ("mileage", "Car mileage", 3, "km", Decimal("0.17000")),
]

DEMO_CLIENT = ("ABC Manufacturing Sdn Bhd", "199001000000")

# The e-Claim expense-category master — the expense-FIRST taxonomy staff actually
# pick from, modelled on the leading systems (SAP Concur ~50, Brex ~80; we keep a
# focused ~23). e-Claim does NO carbon maths — each category carries ONE carbon
# field, ``carbon_relevant`` (= forward this line's raw data to CarbonNext?).
# CarbonNext owns scope/factor/tCO2e. ERP gets EVERY line regardless.
#
#   carbon_relevant True  = forwarded to CarbonNext (travel, fuel, utilities,
#                           meals, goods & services — Scope 1/2/3).
#                   False = non-emitting → ERP only (parking, tolls, bank charges,
#                           training, professional fees, medical).
#   expense_type = the auto-match / merchant-mapping slug (Grab→taxi,
#                  McDonald's→meals, Shell→fuel_petrol, …).
#
# (name, expense_type, carbon_relevant, gl_export_code, default_limit)
DEMO_CATEGORIES = [
    # --- Forwarded to CarbonNext ------------------------------------------------
    ("Fuel — Diesel (fleet)", "fuel_diesel", True, "6200", None),
    ("Fuel — Petrol (fleet)", "fuel_petrol", True, "6200", None),
    ("Electricity", "electricity", True, "6300", None),
    # The OCR emits expense_type natural_gas (unit m3) — without this row such a
    # bill lands unmapped and forwards to CarbonNext with category=NULL.
    ("Natural gas", "natural_gas", True, "6310", None),
    ("Air travel", "air_travel", True, "6410", None),
    ("Mileage — own car", "mileage", True, "6450", None),
    ("Hotel / accommodation", "hotel", True, "6430", Decimal("500")),
    ("Taxi / e-hailing", "taxi", True, "6420", Decimal("100")),
    ("Rail / train", "rail", True, "6421", None),
    ("Public transport", "public_transport", True, "6422", None),
    ("Car rental", "car_rental", True, "6423", None),
    ("Meals", "meals", True, "6100", Decimal("100")),
    ("Business meals & entertainment", "entertainment", True, "6110", Decimal("300")),
    ("Office supplies", "office", True, "6500", Decimal("300")),
    ("Software & subscriptions", "software", True, "6510", None),
    ("Telephone & internet", "telco", True, "6520", None),
    ("Postage & courier", "courier", True, "6530", None),
    # --- ERP reimbursement only (non-emitting; not sent to CarbonNext) ----------
    ("Parking", "parking", False, "6440", Decimal("50")),
    ("Tolls", "tolls", False, "6441", None),
    ("Training & conferences", "training", False, "6600", None),
    ("Professional fees", "professional", False, "6610", None),
    ("Bank charges", "bank_charges", False, "6700", None),
    ("Medical", "medical", False, "6710", None),
    ("Other / miscellaneous", "other", True, "6900", None),
]

# A firm-scoped 'partner' login so the web UI works on a fresh install. The dev
# DevAuthProvider does no password check — this email alone signs in. Partner is
# firm-scoped, so it sees every client in the firm without an explicit grant.
DEMO_USER_EMAIL = "partner@demo.test"
# A SECOND firm user so the maker≠checker flow can be exercised: claims captured by
# the partner are approved by this separate reviewer (separation of duties).
DEMO_APPROVER_EMAIL = "approver@demo.test"


def seed() -> None:
    # Seeding writes firm-scoped rows before any tenant context exists, so it
    # connects on the admin DSN (``DATABASE_URL``) where RLS is bypassed — the
    # same role migrations run as. The app's get_sessionmaker() uses the
    # unprivileged onecapture_app role, for which RLS would hide the existing
    # firm/client and deny every insert.
    engine = create_engine(get_settings().database_url, future=True)
    session = Session(engine, future=True)
    try:
        if session.execute(select(Client).limit(1)).scalar_one_or_none() is None:
            session.add(Client(name=DEMO_CLIENT[0], ssm_no=DEMO_CLIENT[1], currency="MYR"))
        session.flush()

        # Bind the login user to the firm that owns the (migration- or above-)
        # seeded client. Earliest client keeps default_client_id unambiguous.
        client = session.execute(
            select(Client).order_by(Client.created_at).limit(1)
        ).scalar_one()
        # Real-system policy: enforce full posting coding (GL + cost centre) before
        # a claim can be released to accounting. Stored on client.modules.
        client.modules = {**(client.modules or {}), "require_posting_coding": True}
        if (
            session.execute(
                select(AppUser).where(AppUser.email == DEMO_USER_EMAIL)
            ).scalar_one_or_none()
            is None
        ):
            session.add(
                AppUser(
                    firm_id=client.firm_id,
                    email=DEMO_USER_EMAIL,
                    display_name="Demo Partner",
                    base_role="partner",
                )
            )
        # A separate reviewer so a claim the partner captured can be approved by a
        # DIFFERENT user (SoD: the maker can't approve their own claim).
        if (
            session.execute(
                select(AppUser).where(AppUser.email == DEMO_APPROVER_EMAIL)
            ).scalar_one_or_none()
            is None
        ):
            session.add(
                AppUser(
                    firm_id=client.firm_id,
                    email=DEMO_APPROVER_EMAIL,
                    display_name="Demo Approver",
                    base_role="partner",
                )
            )

        for key, label, scope, unit, value in DEMO_FACTORS:
            exists = session.execute(
                select(EmissionFactor).where(
                    EmissionFactor.factor_key == key, EmissionFactor.version == 1
                )
            ).scalar_one_or_none()
            if exists is None:
                session.add(
                    EmissionFactor(
                        factor_key=key,
                        label=label,
                        scope=scope,
                        unit=unit,
                        factor_kg_per_unit=value,
                        source="demo placeholder (D14)",
                        version=1,
                    )
                )
        # Reconcile the category master for the seeded client: UPSERT each master
        # row (by name) and ARCHIVE (status='inactive') any other active category —
        # so an existing demo client with the old messy/duplicate list converges on
        # the clean taxonomy. Archive (not delete) keeps any claim_line FK valid;
        # inactive categories drop out of the capture/review dropdowns.
        master_names = {name for name, *_ in DEMO_CATEGORIES}
        for name, expense_type, carbon_relevant, gl, limit in DEMO_CATEGORIES:
            cat = session.execute(
                select(Category).where(
                    Category.client_id == client.id, Category.name == name
                )
            ).scalar_one_or_none()
            if cat is None:
                session.add(
                    Category(
                        firm_id=client.firm_id, client_id=client.id, name=name,
                        expense_type=expense_type, carbon_relevant=carbon_relevant,
                        gl_export_code=gl, default_limit=limit, status="active",
                    )
                )
            else:
                cat.expense_type = expense_type
                cat.carbon_relevant, cat.gl_export_code = carbon_relevant, gl
                cat.default_limit, cat.status = limit, "active"
        archived = 0
        for cat in session.execute(
            select(Category).where(
                Category.client_id == client.id,
                Category.status == "active",
                Category.name.notin_(master_names),
            )
        ).scalars():
            cat.status = "inactive"
            archived += 1
        session.commit()
        print("  reconciled categories (archived %d non-master)" % archived)
        print(
            "seeded: 1 client, %d factors, %d categories, logins %s (capture) + %s (approve)"
            % (len(DEMO_FACTORS), len(DEMO_CATEGORIES), DEMO_USER_EMAIL, DEMO_APPROVER_EMAIL)
        )
    finally:
        session.close()


if __name__ == "__main__":
    seed()
