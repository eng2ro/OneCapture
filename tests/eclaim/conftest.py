"""Fixtures for the e-Claim DB-backed tests.

* Schema is built from the Alembic migration against ``TEST_DATABASE_URL``
  (not ``create_all``) so a model/migration drift surfaces as a failure.
* Each test runs inside one external transaction rolled back at teardown
  (SQLAlchemy ``join_transaction_mode="create_savepoint"``), so app commits are
  isolated and nothing accumulates.
* OCR is always a fake — the real Anthropic provider never runs in CI.
* If no Postgres test DB is reachable, the whole module SKIPS with a clear note.
"""

from __future__ import annotations

import os

# The optional "share gate" (HTTP Basic front door) is driven by .env, which the
# real app reads. Force it OFF for tests — an env var overrides the .env value —
# before any get_settings() call, so the test client never has to send Basic auth.
os.environ["SHARE_GATE_USER"] = ""
os.environ["SHARE_GATE_PASS"] = ""

# The async ingestion worker is a background thread that would hit the real vision
# model. Tests drive ingestion deterministically (build_claim / process_one with a
# fake provider bundle), so the lifespan worker stays OFF here.
os.environ["OC_DISABLE_INGEST_WORKER"] = "1"

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from fastapi import Request

from eclaim.auth.principal import Principal
from eclaim.config import get_settings
from eclaim.db.models import AppUser, Category, Client, EmissionFactor
from eclaim.ocr.base import Extraction

# Must match the firm id the 0002 migration seeds (deterministic by design): the
# firm/client roster is RLS-gated, so a test connecting as onecapture_app has to
# set this firm context *before* it can read even the default client row.
DEFAULT_FIRM_ID = "11111111-1111-1111-1111-111111111111"

DEMO_FACTORS = [
    ("fuel_diesel", "Diesel", 1, "L", Decimal("2.68000")),
    ("fuel_petrol", "Petrol", 1, "L", Decimal("2.31000")),
    ("electricity", "Grid electricity", 2, "kWh", Decimal("0.58500")),
    ("natural_gas", "Natural gas", 1, "m3", Decimal("2.03000")),
    ("air_travel", "Air travel", 3, "km", Decimal("0.18000")),
]


def _bootstrap_app_role(admin_engine, settings) -> None:
    """Sync the ``onecapture_app`` login password to ``APP_TEST_DATABASE_URL``
    (the single source of truth) right after the migration, so the RLS-enforced
    tests can authenticate as the non-superuser role on a fresh clone/CI.

    The 0002 migration creates the role WITHOUT a password by design. Rather than
    SKIP when it can't authenticate — which would mask untested isolation as a
    green run, the exact silent-skip trap we're closing — we set it here from the
    app DSN, run as the admin/owner connection.

    Guarded: only runs against a local DSN (host localhost/127.0.0.1/::1) or when
    ``OC_TEST_BOOTSTRAP_ROLE=1`` is set, so a plain ``pytest`` run can never ALTER
    a role on a shared/staging cluster. For real CI, lift this into the
    provisioning step — the guard makes the fixture a no-op against remote DSNs.
    """
    app_url = settings.app_test_database_url
    if not app_url:
        return
    url = make_url(app_url)
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    is_local = url.host in local_hosts
    if not is_local and os.environ.get("OC_TEST_BOOTSTRAP_ROLE") != "1":
        return
    role, password = url.username, url.password or ""
    if not role:
        return
    # ALTER ROLE ... PASSWORD takes a literal, not a bind parameter; role + pw are
    # our own controlled values, escaped defensively all the same.
    role_ident = '"' + role.replace('"', '""') + '"'
    pw_literal = "'" + password.replace("'", "''") + "'"
    with admin_engine.begin() as conn:
        conn.execute(text(f"ALTER ROLE {role_ident} PASSWORD {pw_literal}"))


@pytest.fixture(scope="session")
def db_engine():
    """Admin/owner engine: runs the Alembic migration (only the owner can DDL +
    create the role) and bootstraps the app-role password. Owner connection
    bypasses RLS, so it is also what the isolation tests seed committed rows
    with."""
    settings = get_settings()
    url = settings.test_database_url
    # Short connect timeout so the suite skips fast when no DB is running.
    engine = create_engine(url, future=True, connect_args={"connect_timeout": 3})
    try:
        conn = engine.connect()
        conn.close()
    except OperationalError as exc:
        pytest.skip(f"Postgres test DB not reachable at {url!r}: {exc}")

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    # set_main_option stores the value in a ConfigParser, which treats '%' as
    # interpolation syntax. A percent-encoded URL (e.g. a password with '%40')
    # must be escaped as '%%'; ConfigParser decodes it back on read.
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    _bootstrap_app_role(engine, settings)

    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine(db_engine):
    """Engine bound to the unprivileged ``onecapture_app`` role, for which RLS is
    actually enforced. Depends on ``db_engine`` so the migration (which creates
    the role) and the password bootstrap have already run. Skips cleanly when the
    app DSN is unset or unconnectable."""
    url = get_settings().app_test_database_url
    if not url:
        pytest.skip("APP_TEST_DATABASE_URL not set — RLS-enforced tests need the onecapture_app DSN")
    # NullPool: every connect() is a fresh backend, so a checked-out connection
    # always starts with NO tenant context (current_firm/allowed_clients unset →
    # default-deny). Without it, a test that sets session-level GUCs
    # (set_config(..., is_local=false), which ROLLBACK does not clear) would leak
    # that context onto a pooled connection a later test reuses — e.g. a leaked
    # empty current_firm makes the firm-match cast ''::uuid and error.
    engine = create_engine(
        url, future=True, poolclass=NullPool, connect_args={"connect_timeout": 3}
    )
    try:
        engine.connect().close()
    except OperationalError as exc:
        pytest.skip(f"onecapture_app not connectable at {url!r}: {exc}")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(app_engine) -> Session:
    """A session on the unprivileged ``onecapture_app`` connection (so RLS bites,
    same as the running app), inside one outer transaction rolled back at
    teardown.

    Tenant context is set on the *outer* transaction — on the raw connection,
    before the savepoint-mode Session opens its first savepoint. Values set with
    ``set_config(is_local=true)`` before a savepoint are NOT reverted when an
    inner savepoint rolls back (only changes made inside it are), so context
    survives a request whose savepoint rolls back (the error-path tests) while
    staying transaction-local: it dies with ``trans.rollback()`` and never leaks
    across pooled connections. Order is forced by the RLS ``WITH CHECK``
    policies — firm context first (every tenant table is gated, including the
    client roster the seed reads), then widen to the committed default client so
    the seeded rows pass the insert check."""
    connection = app_engine.connect()
    trans = connection.begin()
    connection.execute(
        text("SELECT set_config('app.current_firm', :v, true)"), {"v": DEFAULT_FIRM_ID}
    )
    default_client_id = connection.execute(
        text("SELECT id FROM client ORDER BY created_at LIMIT 1")
    ).scalar_one()
    connection.execute(
        text("SELECT set_config('app.allowed_clients', :v, true)"),
        {"v": str(default_client_id)},
    )
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
        future=True,
    )
    _seed(session)
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


def _seed(session: Session) -> None:
    """Seed per-test data on top of the migrated schema.

    The 0002 migration already created the default firm and its default client;
    we reuse that client rather than inserting a second one (so
    ``default_client_id`` stays unambiguous), derive the firm from it, add the
    demo factors, and seed one firm-scoped ``partner`` user. The resolved
    firm/client/user ids are stashed on ``session.info`` for the principal
    override the API tests run under (post-0002, ``client.firm_id`` is NOT NULL,
    so an unbound insert would now fail — this is why the seed binds to a firm)."""
    client = session.execute(
        select(Client).order_by(Client.created_at).limit(1)
    ).scalar_one()
    firm_id = client.firm_id

    user = AppUser(
        firm_id=firm_id,
        email="partner@seed.test",
        display_name="Seed Partner",
        base_role="partner",
    )
    session.add(user)
    for key, label, scope, unit, value in DEMO_FACTORS:
        session.add(
            EmissionFactor(
                factor_key=key, label=label, scope=scope, unit=unit,
                factor_kg_per_unit=value, source="test", version=1,
            )
        )
    session.flush()

    # Category master keyed by the OCR expense-type vocabulary, all carbon-relevant
    # (forwarded to CarbonNext). e-Claim does no carbon maths — a category just
    # carries ``carbon_relevant``. "other" gets no category at all (the unmapped
    # path); tests add their own 'none'/spend categories as needed.
    for key, label, *_ in DEMO_FACTORS:
        session.add(
            Category(
                firm_id=firm_id, client_id=client.id, name=label, expense_type=key,
                carbon_relevant=True,
            )
        )
    session.flush()
    session.info["principal"] = {"firm": firm_id, "client": client.id, "user": user.id}


class FakeOcr:
    """Injectable OCR provider; tests set ``.extraction`` before uploading."""

    def __init__(self) -> None:
        self.extraction = Extraction(expense_type="other", total_amount=Decimal("100"))

    def extract(self, image_bytes: bytes, media_type: str) -> Extraction:
        return self.extraction


@pytest.fixture
def fake_ocr() -> FakeOcr:
    return FakeOcr()


class FakeSegmenter:
    """Injectable page-segmenter; defaults to one page per group (so PDF tests stay
    deterministic). A test sets ``.groups`` to force a specific grouping."""

    def __init__(self) -> None:
        self.groups = None

    def segment(self, pages):
        if self.groups is not None:
            return self.groups
        return [[i] for i in range(len(pages))]


@pytest.fixture
def fake_segmenter() -> FakeSegmenter:
    return FakeSegmenter()


@pytest.fixture
def client(db_session, fake_ocr, fake_segmenter, tmp_path):
    from fastapi.testclient import TestClient

    from eclaim.api import deps
    from eclaim.api.app import create_app

    def _override_db():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    def _principal(request: Request) -> Principal:
        # Firm-scoped partner bound to the default firm/client, mirroring a
        # logged-in firm user. Firm-scoped so it can access the default client;
        # uploads leave created_by_user_id null, so approving here never trips
        # the SoD self-approval rule.
        ids = db_session.info["principal"]
        principal = Principal(
            user_id=ids["user"],
            firm_id=ids["firm"],
            base_role="partner",
            allowed_client_ids=frozenset({ids["client"]}),
            email="partner@seed.test",
        )
        # Mirror the real get_session_principal so the nav context processor can
        # render the sidebar chrome (Admin section, badge counts, tenant scope).
        request.state.principal = principal
        request.state.db = db_session
        return principal

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    app.dependency_overrides[deps.get_principal] = _principal           # API bearer path
    app.dependency_overrides[deps.get_session_principal] = _principal   # web cookie path
    app.dependency_overrides[deps.get_ocr] = lambda: fake_ocr
    app.dependency_overrides[deps.get_segmenter] = lambda: fake_segmenter
    app.dependency_overrides[deps.get_image_dir] = lambda: tmp_path
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def browser(db_session, fake_ocr, fake_segmenter, tmp_path):
    """A TestClient with the SAME db/ocr overrides as ``client`` but NO principal
    override — so the real cookie-session auth runs end to end (login mints the
    cookie; pages resolve the principal from it; no cookie redirects to /login)."""
    from fastapi.testclient import TestClient

    from eclaim.api import deps
    from eclaim.api.app import create_app

    def _override_db():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    app.dependency_overrides[deps.get_ocr] = lambda: fake_ocr
    app.dependency_overrides[deps.get_segmenter] = lambda: fake_segmenter
    app.dependency_overrides[deps.get_image_dir] = lambda: tmp_path
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
