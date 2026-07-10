"""Populate the demo company with realistic UAT data: ~50 employees, the login
users needed to run the flow, and 1–2 claims per employee across a spread of
dates and statuses (parked-for-verification / awaiting-review / approved /
rejected).

Runs against the admin DSN (RLS bypassed, like scripts/seed.py) but drives the
real ClaimService, so every claim's totals, separation-of-duties and audit chain
are correct. Idempotent: employees/users are keyed by email; claim generation is
skipped once seeded claims exist (tagged in remarks).

    python scripts/seed_uat.py
"""

from __future__ import annotations

import datetime as dt
import tempfile
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from eclaim.auth.principal import Principal
from eclaim.config import get_settings
from eclaim.db.models import AppUser, Category, Claim, Claimant, Client
from eclaim.ocr.base import Extraction
from eclaim.services.claims import ClaimService, Repos
from eclaim.services.ingestion import _FormOcr

SEED_TAG = "[UAT-SEED]"
# The OCR Extraction.expense_type is a limited literal (carbon slugs + 'other').
_OCR_TYPES = {"fuel_diesel", "fuel_petrol", "electricity", "natural_gas", "air_travel", "other"}

# --- people ---------------------------------------------------------------- #
_FIRST = [
    "Aina", "Bala", "Mei", "Siti", "Kumar", "Wei Jie", "Nurul", "Arjun", "Li Hua",
    "Faizal", "Priya", "Chong", "Hafiz", "Deepa", "Yong", "Aisyah", "Ramesh",
    "Xin Yi", "Zainab", "Suresh", "Mun Yee", "Farah", "Ganesh", "Wan Ling",
    "Iskandar", "Kavita", "Boon Hock", "Nadia", "Vijay", "Hui Ying", "Rizal",
    "Anitha", "Kok Wai", "Salmah", "Prakash", "Jia Xin", "Amir", "Lakshmi",
    "Teck Seng", "Rohana", "Dinesh", "Pei Shan", "Shahrul", "Meera", "Cheng Kai",
    "Halim", "Divya", "Wai Kit", "Norhayati", "Baskaran",
]
_LAST = [
    "Rahman", "Krishnan", "Tan", "Abdullah", "Nair", "Lim", "Ismail", "Menon",
    "Wong", "Bakar", "Raj", "Lee", "Hassan", "Pillai", "Ng", "Yusof", "Kumar",
    "Chan", "Ali", "Reddy", "Goh", "Sulaiman", "Iyer", "Teoh", "Zulkifli",
]
_DEPTS = [
    ("Sales", "CC-SALES", ["Sales Executive", "Account Manager", "Sales Coordinator"]),
    ("Operations", "CC-OPS", ["Site Engineer", "Operations Executive", "Supervisor"]),
    ("Finance", "CC-FIN", ["Accounts Executive", "Finance Analyst", "AP Clerk"]),
    ("Marketing", "CC-MKT", ["Marketing Executive", "Content Specialist", "Brand Manager"]),
    ("Engineering", "CC-ENG", ["Software Engineer", "QA Engineer", "Project Lead"]),
    ("Admin", "CC-ADMIN", ["Admin Manager", "Office Assistant", "HR Executive"]),
    ("Logistics", "CC-LOG", ["Logistics Coordinator", "Fleet Officer", "Warehouse Lead"]),
]

# Login users needed to run the flow. Makers (capture staff) are approvers; the
# checkers are managers/partners so maker≠checker + the approval matrix both bite.
_USERS = [
    ("manager2@demo.test", "Farah Manager", "manager", None),
    ("siti.staff@demo.test", "Siti Capture", "approver", None),
    ("kumar.staff@demo.test", "Kumar Capture", "approver", None),
    ("wei.staff@demo.test", "Wei Capture", "approver", None),
    ("viewer@demo.test", "Read Only", "viewer", None),
]

# (category name, min RM, max RM, quantity-bearing?) — a realistic spread.
_SPEND = [
    ("Meals", 18, 120, None),
    ("Taxi / e-hailing", 12, 85, None),
    ("Hotel / accommodation", 180, 620, None),
    ("Office supplies", 25, 340, None),
    ("Fuel — Petrol (fleet)", 60, 240, ("L", 30, 120)),
    ("Parking", 5, 40, None),
    ("Mileage — own car", 40, 380, ("km", 60, 520)),
    ("Air travel", 350, 2800, ("km", 300, 3000)),
    ("Telephone & internet", 80, 260, None),
    ("Training & conferences", 400, 6500, None),
    ("Business meals & entertainment", 120, 900, None),
    ("Electricity", 220, 3200, ("kWh", 400, 6000)),
]


def _deterministic(n: int, lo: int, hi: int) -> int:
    """A stable pseudo-value in [lo, hi] from n — no Math.random, reproducible."""
    return lo + (n * 2654435761 % (hi - lo + 1))


def run() -> None:
    engine = create_engine(get_settings().database_url, future=True)
    session = Session(engine, future=True)
    svc = ClaimService()
    repos = Repos.for_session(session)
    image_dir = Path(tempfile.mkdtemp(prefix="uat_seed_"))
    try:
        client = session.execute(
            select(Client).order_by(Client.created_at).limit(1)
        ).scalar_one()
        firm_id = client.firm_id

        # ---- employees (top up to 50 total) -------------------------------- #
        have = session.execute(
            select(func.count()).select_from(Claimant).where(Claimant.client_id == client.id)
        ).scalar_one()
        made_emp = 0
        idx = have
        while session.execute(
            select(func.count()).select_from(Claimant).where(Claimant.client_id == client.id)
        ).scalar_one() < 50:
            fn = _FIRST[idx % len(_FIRST)]
            ln = _LAST[(idx * 7) % len(_LAST)]
            dept, cc, positions = _DEPTS[idx % len(_DEPTS)]
            ref = f"E-{idx + 1:03d}"
            email = f"emp{idx + 1:03d}@demo.test"
            if session.execute(
                select(Claimant).where(Claimant.client_id == client.id, Claimant.email == email)
            ).scalar_one_or_none() is None:
                session.add(Claimant(
                    firm_id=firm_id, client_id=client.id,
                    name=f"{fn} {ln}", phone=f"+601{_deterministic(idx, 10000000, 99999999)}",
                    email=email, employee_ref=ref, cost_centre=cc,
                    position=positions[idx % len(positions)], department=dept,
                    status="active",
                ))
                made_emp += 1
            idx += 1
        session.commit()

        # ---- login users --------------------------------------------------- #
        made_users = 0
        for email, name, role, cap in _USERS:
            if session.execute(
                select(AppUser).where(AppUser.firm_id == firm_id, AppUser.email == email)
            ).scalar_one_or_none() is None:
                session.add(AppUser(
                    firm_id=firm_id, email=email, display_name=name,
                    base_role=role, authority_limit=cap, status="active",
                ))
                made_users += 1
        session.commit()

        # ---- claims -------------------------------------------------------- #
        already = session.execute(
            select(func.count()).select_from(Claim).where(Claim.remarks.like(f"%{SEED_TAG}%"))
        ).scalar_one()
        if already:
            print(f"employees: {made_emp} new · users: {made_users} new · "
                  f"claims: skipped ({already} seed claims already present)")
            return

        # Makers (capture staff) and checkers (approve/reject) as Principals.
        staff = session.execute(
            select(AppUser).where(AppUser.firm_id == firm_id,
                                  AppUser.email.in_([u[0] for u in _USERS if u[2] == "approver"]))
        ).scalars().all()
        partner = session.execute(
            select(AppUser).where(AppUser.firm_id == firm_id, AppUser.base_role == "partner")
        ).scalars().first()
        manager = session.execute(
            select(AppUser).where(AppUser.firm_id == firm_id, AppUser.email == "manager@demo.test")
        ).scalar_one_or_none() or session.execute(
            select(AppUser).where(AppUser.firm_id == firm_id, AppUser.base_role == "manager")
        ).scalars().first()

        def principal(u: AppUser) -> Principal:
            return Principal(user_id=u.id, firm_id=firm_id, base_role=u.base_role,
                             allowed_client_ids=frozenset({client.id}),
                             authority_limit=u.authority_limit, email=u.email)

        partner_p = principal(partner)
        manager_p = principal(manager)
        cats = {c.name: c for c in session.execute(
            select(Category).where(Category.client_id == client.id, Category.status == "active")
        ).scalars()}

        employees = session.execute(
            select(Claimant).where(Claimant.client_id == client.id, Claimant.status == "active")
        ).scalars().all()
        today = dt.date.today()
        counts = {"submitted": 0, "in_review": 0, "approved": 0, "rejected": 0}
        n = 0
        for e_i, emp in enumerate(employees):
            for rec in range(1 + (e_i % 2)):          # 1 or 2 claims each
                spend = _SPEND[n % len(_SPEND)]
                cat = cats.get(spend[0])
                if cat is None:
                    n += 1
                    continue
                amount = Decimal(_deterministic(n * 3 + rec, spend[1], spend[2]))
                days_ago = _deterministic(n * 5 + 1, 2, 90)
                d = today - dt.timedelta(days=days_ago)
                maker = staff[n % len(staff)]

                # The OCR expense_type is a limited carbon-slug literal; the real
                # category is set explicitly via category_id below, so map anything
                # non-carbon to 'other'.
                et = cat.expense_type if cat.expense_type in _OCR_TYPES else "other"
                ext = Extraction(
                    expense_type=et, total_amount=amount, currency="MYR",
                    vendor=f"{spend[0].split(' ')[0]} Vendor {n % 40 + 1}",
                    date=d.strftime("%d %b %Y"),
                )
                if spend[3]:
                    unit, qlo, qhi = spend[3]
                    ext = ext.model_copy(update={
                        "quantity": Decimal(_deterministic(n * 9, qlo, qhi)), "unit": unit})

                claim = svc.start_claim(
                    repos=repos, firm_id=firm_id, client_id=client.id,
                    title=f"{spend[0]} — {emp.name.split(' ')[0]}",
                    purpose="Business expense", posting_date=d,
                    created_by_user_id=maker.id,
                    submitted_by_claimant_id=emp.id,
                )
                claim.remarks = SEED_TAG
                svc.add_line(
                    repos=repos, claim=claim, image_bytes=b"\x89PNG " + str(n).encode(),
                    media_type="image/png", ocr=_FormOcr(ext), image_dir=image_dir,
                    category_id=cat.id, payment_method="out_of_pocket",
                )

                # Status mix by index: park a few for verification, leave many in
                # review, approve a good share, reject a few.
                bucket = n % 20
                if bucket < 3:                        # parked for verification
                    svc.submit(repos=repos, claim=claim, actor=maker.email, line_count=1,
                               attested=True, self_verified=False)
                    counts["submitted"] += 1
                else:
                    svc.submit(repos=repos, claim=claim, actor=maker.email, line_count=1,
                               attested=True, self_verified=True)
                    if bucket < 11:                   # awaiting review
                        counts["in_review"] += 1
                    elif bucket < 18:                 # approved (manager ≤2k, else partner)
                        checker = manager_p if amount <= Decimal("2000") else partner_p
                        svc.approve(repos=repos, claim_id=claim.id,
                                    actor=checker.email, approver=checker)
                        counts["approved"] += 1
                    else:                             # rejected
                        svc.reject(repos=repos, claim_id=claim.id, reviewer=partner_p,
                                   reason="Out of policy — no itemised receipt")
                        counts["rejected"] += 1

                # Backdate the visible date to the transaction date.
                claim.created_at = dt.datetime.combine(d, dt.time(9, 0), dt.timezone.utc)
                session.flush()
                n += 1
        session.commit()
        print(f"employees: {made_emp} new (50 total) · users: {made_users} new")
        print(f"claims: {n} created — {counts}")
    finally:
        session.close()


if __name__ == "__main__":
    run()
