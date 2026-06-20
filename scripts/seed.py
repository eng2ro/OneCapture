"""Seed demo reference data: the emission-factor library + one client.

Run after ``alembic upgrade head``::

    python scripts/seed.py

Idempotent: re-running won't duplicate the demo client or factors. Factor
values are demo placeholders (decision D14) until the carbon lead sets the real
set.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from eclaim.db.models import Client, EmissionFactor
from eclaim.db.session import get_sessionmaker

# factor_key, label, scope, unit, kgCO2e/unit
DEMO_FACTORS = [
    ("fuel_diesel", "Diesel (fleet)", 1, "L", Decimal("2.68000")),
    ("fuel_petrol", "Petrol (fleet)", 1, "L", Decimal("2.31000")),
    ("electricity", "Grid electricity (MY)", 2, "kWh", Decimal("0.58500")),
    ("natural_gas", "Natural gas", 1, "m3", Decimal("2.03000")),
    ("air_travel", "Air travel", 3, "km", Decimal("0.18000")),
]

DEMO_CLIENT = ("ABC Manufacturing Sdn Bhd", "199001000000")


def seed() -> None:
    session = get_sessionmaker()()
    try:
        if session.execute(select(Client).limit(1)).scalar_one_or_none() is None:
            session.add(Client(name=DEMO_CLIENT[0], ssm_no=DEMO_CLIENT[1], currency="MYR"))

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
        session.commit()
        print("seeded: 1 client, %d factors" % len(DEMO_FACTORS))
    finally:
        session.close()


if __name__ == "__main__":
    seed()
