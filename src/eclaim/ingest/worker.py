"""In-process ingestion worker: drains the ``ingestion_job`` queue.

A single background thread (started in the app lifespan) polls for queued jobs,
claims one at a time with ``FOR UPDATE SKIP LOCKED`` — so it's safe even if the app
is later run with several uvicorn workers — and builds the claim via
:func:`eclaim.services.ingestion.build_claim`, reporting progress on the job row.

Durability: the claim is only written in build_claim's final atomic transaction, so
a crash mid-read leaves no partial claim. A job left ``running`` by a dead worker is
reclaimed once its heartbeat goes stale and simply re-run from scratch. Because the
claim query admits the trusted worker context (``app.worker='on'``), the worker can
see queued rows across tenants; every downstream write uses the real per-job tenant
context, so it stays strictly scoped.
"""

from __future__ import annotations

import logging
import threading
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db.session import get_sessionmaker
from ..services import ingestion
from ..services.claims import Repos
from ..tenancy import set_tenant_context

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0
# A running job whose worker died: reclaim it once the heartbeat is this stale.
STALE_INTERVAL = "5 minutes"

_CLAIM_SQL = text(f"""
    SELECT id, firm_id, client_id, payload
    FROM ingestion_job
    WHERE status = 'queued'
       OR (status = 'running'
           AND (heartbeat_at IS NULL OR heartbeat_at < now() - interval '{STALE_INTERVAL}'))
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
""")


def default_providers() -> ingestion.Providers:
    """The real providers the background worker reads the world through."""
    from ..api import deps

    return ingestion.Providers(
        ocr=deps.get_ocr(),
        segmenter=deps.get_segmenter(),
        image_dir=deps.get_image_dir(),
        mileage_rate=deps.get_mileage_rate(),
        directions=deps.get_directions(),
    )


def _claim_next(session: Session) -> dict | None:
    """Atomically claim the next runnable job, mark it running, and return its
    ``{id, firm_id, client_id, payload}`` — or None if the queue is empty."""
    session.execute(text("SELECT set_config('app.worker', 'on', true)"))
    row = session.execute(_CLAIM_SQL).mappings().first()
    if row is None:
        session.rollback()
        return None
    session.execute(
        text("UPDATE ingestion_job SET status='running', attempts=attempts+1, "
             "heartbeat_at=now() WHERE id=:i"),
        {"i": row["id"]},
    )
    session.commit()
    return dict(row)


def _finish(session: Session, job_id, status: str, *, claim_id=None, error: str | None = None) -> None:
    sql = "UPDATE ingestion_job SET status=:s, error=:e, heartbeat_at=now()"
    vals: dict = {"s": status, "e": error, "i": job_id}
    if claim_id is not None:
        sql += ", claim_id=:c"
        vals["c"] = str(claim_id)
    if status == "done":
        sql += ", done_units=total_units"   # make the progress bar read complete
    sql += " WHERE id=:i"
    session.execute(text(sql), vals)
    session.commit()


def _existing_claim_id(session: Session, job_id) -> uuid.UUID | None:
    """The claim already built for this job, if any. A crash between the claim
    commit and the job being marked done leaves the job re-claimable; on re-run we
    find the existing claim here and re-mark the job done instead of rebuilding
    (no duplicate claim, no re-billed OCR). Runs under the job's tenant context so
    RLS scopes it."""
    return session.execute(
        text("SELECT id FROM claim WHERE ingestion_job_id = :j"), {"j": str(job_id)}
    ).scalar()


def process_one(session: Session, providers: ingestion.Providers) -> uuid.UUID | None:
    """Claim and run at most one job on ``session``. Returns the job id it handled,
    or None if the queue was empty. Never raises — a failing job is marked failed."""
    claimed = _claim_next(session)
    if claimed is None:
        return None
    job_id = claimed["id"]
    snap = claimed["payload"]["snapshot"]
    firm_id = uuid.UUID(snap["firm_id"])
    allowed = [uuid.UUID(c) for c in snap["allowed_client_ids"]]
    user_id = uuid.UUID(snap["created_by_user_id"]) if snap["created_by_user_id"] else None

    def _progress(done: int, total: int) -> None:
        # Committed on its own so the progress bar advances during the slow read
        # phase; build_claim only calls this BEFORE its atomic write phase, so this
        # never commits a half-built claim. Re-arm the tenant context afterwards.
        session.execute(
            text("UPDATE ingestion_job SET done_units=:d, total_units=:t, heartbeat_at=now() "
                 "WHERE id=:i"),
            {"d": done, "t": total, "i": job_id},
        )
        session.commit()
        set_tenant_context(session, firm_id, allowed)

    try:
        set_tenant_context(session, firm_id, allowed)
        # Idempotent completion (B3): if a prior run already built this job's claim
        # but died before marking the job done, re-mark it done for that claim —
        # never rebuild (no duplicate, no re-billed OCR, staging may already be gone).
        prior = _existing_claim_id(session, job_id)
        if prior is not None:
            _finish(session, job_id, "done", claim_id=prior)
            ingestion.cleanup_staging(providers.image_dir, job_id)
            return job_id

        staged = ingestion.read_staged(providers.image_dir, job_id, claimed["payload"]["manifest"])
        set_tenant_context(session, firm_id, allowed)
        result = ingestion.build_claim(
            Repos.for_session(session), providers,
            firm_id=firm_id, client_id=uuid.UUID(snap["client_id"]),
            created_by_user_id=user_id, allowed_client_ids=allowed,
            header=claimed["payload"]["header"], staged=staged,
            item_list=claimed["payload"]["items"], mileage_specs=claimed["payload"]["mileage"],
            split_docs=claimed["payload"]["split_docs"], on_progress=_progress,
            # Key the claim to this job and DON'T commit yet: _finish below flips the
            # job to done in the SAME transaction, so claim + completion are atomic.
            ingestion_job_id=job_id, commit=False,
        )
        set_tenant_context(session, firm_id, allowed)
        if result.header_error:
            _finish(session, job_id, "failed", error=result.header_error)
        elif result.added == 0:
            _finish(session, job_id, "failed",
                    error="Could not add any line. " + ingestion.summarize_errors(result.errors))
        else:
            _finish(session, job_id, "done", claim_id=result.claim_id,
                    error=(ingestion.summarize_errors(result.errors) or None))
        ingestion.cleanup_staging(providers.image_dir, job_id)
    except Exception as exc:   # never let one job kill the worker
        logger.exception("ingestion job %s crashed", job_id)
        session.rollback()
        set_tenant_context(session, firm_id, allowed)
        # If a concurrent/previous run already committed this job's claim (e.g. a
        # UNIQUE ingestion_job_id collision), reconcile to done — don't fail a job
        # that actually has a valid claim. Otherwise mark failed (terminal, not
        # reclaimed) so its staged files can go too.
        prior = _existing_claim_id(session, job_id)
        if prior is not None:
            _finish(session, job_id, "done", claim_id=prior)
        else:
            _finish(session, job_id, "failed", error=f"ingestion failed: {type(exc).__name__}: {exc}")
        ingestion.cleanup_staging(providers.image_dir, job_id)
    return job_id


# A staging dir this old whose job is terminal/absent is an orphan (e.g. enqueued
# but the worker never ran, or the row was deleted). Swept periodically.
STAGING_TTL_HOURS = 24


def sweep_staging(session: Session, image_dir, ttl_hours: int = STAGING_TTL_HOURS) -> int:
    """Remove staging dirs older than the TTL whose job is done/failed/absent. Never
    touches a queued/running job's files (a reclaim would still need them). Returns
    the number of dirs removed."""
    import shutil
    import time
    from pathlib import Path

    root = Path(image_dir) / "staging"
    if not root.exists():
        return 0
    cutoff = time.time() - ttl_hours * 3600
    stale = []
    for d in root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime <= cutoff:
                stale.append(d)
        except OSError:
            continue
    if not stale:
        return 0
    removed = 0
    session.execute(text("SELECT set_config('app.worker', 'on', true)"))
    for d in stale:
        try:
            uuid.UUID(d.name)
            row = session.execute(
                text("SELECT status FROM ingestion_job WHERE id = :i"), {"i": d.name}
            ).first()
            terminal = row is None or row[0] in ("done", "failed")
        except ValueError:
            terminal = True   # not a job-id dir but old — an orphan
        if terminal:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    session.rollback()
    if removed:
        logger.info("staging janitor removed %d orphaned dir(s)", removed)
    return removed


class Worker:
    """The background poller thread. ``start()`` in the app lifespan, ``stop()`` on
    shutdown. ``providers`` is injectable for tests; the real ones are built lazily."""

    def __init__(self, providers: ingestion.Providers | None = None) -> None:
        self._providers = providers
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="oc-ingest-worker", daemon=True)
        self._thread.start()
        logger.info("ingestion worker started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        providers = self._providers or default_providers()
        session_factory = get_sessionmaker()
        ticks = 0
        # Sweep once at startup, then roughly every 30 minutes of wall time.
        _SWEEP_EVERY = int(30 * 60 / POLL_SECONDS)
        while not self._stop.is_set():
            session = session_factory()
            try:
                handled = process_one(session, providers)
                if ticks % _SWEEP_EVERY == 0:
                    try:
                        sweep_staging(session, providers.image_dir)
                    except Exception:
                        logger.exception("staging janitor error")
            except Exception:   # a claim/DB error — back off, don't spin
                logger.exception("ingestion worker loop error")
                handled = None
            finally:
                session.close()
            ticks += 1
            if handled is None:
                self._stop.wait(POLL_SECONDS)
