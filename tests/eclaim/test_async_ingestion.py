"""Async ingestion: a large upload is staged + queued, then the in-process worker
builds the claim in the background (DB-backed, fake OCR/segmenter).

A small upload stays inline (covered by test_web_capture*); here we force the async
path (more pages than ``INLINE_MAX_UNITS``) and drive the worker's ``process_one``
directly — the lifespan worker thread is off in tests."""

from __future__ import annotations

import os
import re
import time
import uuid
from decimal import Decimal

from fpdf import FPDF
from sqlalchemy import select, text

from eclaim.db.models import Claim, ClaimLine, Client, DocumentIntake, IngestionJob
from eclaim.ingest import worker
from eclaim.ocr.base import Extraction
from eclaim.services import ingestion


def _pdf(pages: list[str]) -> bytes:
    doc = FPDF()
    for text in pages:
        doc.add_page()
        doc.set_font("Helvetica", size=20)
        doc.cell(0, 20, text)
    return bytes(doc.output())


def _enable_split(db_session):
    cid = db_session.info["principal"]["client"]
    cl = db_session.get(Client, cid)
    cl.modules = {**(cl.modules or {}), "allow_document_split": True}
    db_session.flush()


def _providers(fake_ocr, fake_segmenter, tmp_path):
    return ingestion.Providers(
        ocr=fake_ocr, segmenter=fake_segmenter, image_dir=tmp_path,
        mileage_rate=Decimal("0.60"), directions=None,
    )


def _enqueue_via_http(client, pages: int):
    resp = client.post(
        "/capture",
        files=[("files", ("invoices.pdf", _pdf([f"Invoice {i}" for i in range(pages)]),
                          "application/pdf"))],
        data={"items": "[null]", "attested": "yes"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text[:300]
    m = re.match(r"^/ingest/([0-9a-f-]+)$", resp.headers["location"])
    assert m, resp.headers["location"]        # a big upload goes to the progress page
    return uuid.UUID(m.group(1))


def test_small_upload_stays_inline(client):
    # 2 pages (<= INLINE_MAX_UNITS) → built inline, straight to the review screen.
    resp = client.post(
        "/capture",
        files=[("files", ("inv.pdf", _pdf(["A", "B"]), "application/pdf"))],
        data={"items": "[null]", "attested": "yes"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert re.match(r"^/claims/[0-9a-f-]+/review$", resp.headers["location"])


def test_large_upload_is_queued_not_built_yet(client, db_session):
    job_id = _enqueue_via_http(client, 5)
    job = db_session.get(IngestionJob, job_id)
    assert job is not None
    assert job.status == "queued"
    assert job.total_units == 5          # cheap page-count estimate
    assert job.claim_id is None          # nothing built until the worker runs
    # No claim exists yet.
    assert db_session.execute(select(Claim)).scalars().all() == []


def test_worker_builds_the_claim(client, db_session, fake_ocr, fake_segmenter, tmp_path):
    _enable_split(db_session)             # so each page becomes its own line
    job_id = _enqueue_via_http(client, 4)

    handled = worker.process_one(db_session, _providers(fake_ocr, fake_segmenter, tmp_path))
    assert handled == job_id

    db_session.expire_all()
    job = db_session.get(IngestionJob, job_id)
    assert job.status == "done"
    assert job.claim_id is not None
    lines = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == job.claim_id)
    ).scalars().all()
    assert len(lines) == 4               # one line per page (split on, fake one-per-page)


def test_status_endpoint_tracks_the_job(client, db_session, fake_ocr, fake_segmenter, tmp_path):
    job_id = _enqueue_via_http(client, 5)

    before = client.get(f"/ingest/{job_id}/status").json()
    assert before["state"] == "queued"
    assert before["redirect"] is None

    worker.process_one(db_session, _providers(fake_ocr, fake_segmenter, tmp_path))

    after = client.get(f"/ingest/{job_id}/status").json()
    assert after["state"] == "done"
    assert after["redirect"] == f"/claims/{db_session.get(IngestionJob, job_id).claim_id}/review"


def test_progress_page_redirects_when_done(client, db_session, fake_ocr, fake_segmenter, tmp_path):
    job_id = _enqueue_via_http(client, 5)
    worker.process_one(db_session, _providers(fake_ocr, fake_segmenter, tmp_path))
    page = client.get(f"/ingest/{job_id}", follow_redirects=False)
    assert page.status_code == 303
    assert re.match(r"^/claims/[0-9a-f-]+/review$", page.headers["location"])


def test_worker_returns_none_when_queue_empty(db_session, fake_ocr, fake_segmenter, tmp_path):
    assert worker.process_one(db_session, _providers(fake_ocr, fake_segmenter, tmp_path)) is None


def _force_running(db_session, job_id, *, attempts: int):
    """Put a job into the stale-'running' state a dead worker leaves behind."""
    db_session.execute(
        text("UPDATE ingestion_job SET status='running', attempts=:a, "
             "heartbeat_at = now() - interval '10 minutes' WHERE id=:i"),
        {"a": attempts, "i": str(job_id)},
    )
    db_session.commit()


def test_poison_job_dead_lettered_after_max_attempts(
    client, db_session, fake_ocr, fake_segmenter, tmp_path
):
    """B4: a job that has crashed the worker MAX_ATTEMPTS times is not re-claimed
    again — it is dead-lettered to failed, so it can't loop forever re-billing OCR."""
    job_id = _enqueue_via_http(client, 4)
    provs = _providers(fake_ocr, fake_segmenter, tmp_path)
    _force_running(db_session, job_id, attempts=worker.MAX_ATTEMPTS)

    worker.process_one(db_session, provs)
    db_session.expire_all()

    job = db_session.get(IngestionJob, job_id)
    assert job.status == "failed"
    assert "crashed the worker" in (job.error or "")
    assert job.claim_id is None
    assert db_session.execute(
        select(Claim).where(Claim.ingestion_job_id == job_id)
    ).scalars().all() == []          # no claim was built on the exhausted job


def test_stale_running_job_below_cap_is_still_reclaimed(
    client, db_session, fake_ocr, fake_segmenter, tmp_path
):
    """A real worker crash / deploy below the cap must NOT lose the job — a stale
    'running' job under MAX_ATTEMPTS is still retried."""
    _enable_split(db_session)
    job_id = _enqueue_via_http(client, 4)
    provs = _providers(fake_ocr, fake_segmenter, tmp_path)
    _force_running(db_session, job_id, attempts=worker.MAX_ATTEMPTS - 1)

    assert worker.process_one(db_session, provs) == job_id   # reclaimed + built
    db_session.expire_all()
    assert db_session.get(IngestionJob, job_id).status == "done"


def test_built_claim_is_keyed_to_its_job(client, db_session, fake_ocr, fake_segmenter, tmp_path):
    """B3: an async-built claim carries its ingestion_job_id (the idempotency key)."""
    job_id = _enqueue_via_http(client, 4)
    worker.process_one(db_session, _providers(fake_ocr, fake_segmenter, tmp_path))
    db_session.expire_all()
    claim_id = db_session.get(IngestionJob, job_id).claim_id
    assert db_session.get(Claim, claim_id).ingestion_job_id == job_id


def test_reclaimed_job_does_not_duplicate_claim(client, db_session, fake_ocr, fake_segmenter, tmp_path):
    """B3: a job re-claimed AFTER its claim was built (worker crashed before marking
    the job done) must recover the existing claim, never build a second one."""
    _enable_split(db_session)
    job_id = _enqueue_via_http(client, 4)
    provs = _providers(fake_ocr, fake_segmenter, tmp_path)

    assert worker.process_one(db_session, provs) == job_id
    db_session.expire_all()
    first_claim = db_session.get(IngestionJob, job_id).claim_id
    assert first_claim is not None

    # Simulate a crash between the claim commit and the job being marked done:
    # job back to running, heartbeat stale, claim_id cleared → re-claimable.
    db_session.execute(
        text("UPDATE ingestion_job SET status='running', claim_id=NULL, "
             "heartbeat_at = now() - interval '10 minutes' WHERE id = :i"),
        {"i": str(job_id)},
    )
    db_session.commit()

    # Re-run the worker: it must recover the existing claim, not rebuild.
    assert worker.process_one(db_session, provs) == job_id
    db_session.expire_all()

    claims = db_session.execute(
        select(Claim).where(Claim.ingestion_job_id == job_id)
    ).scalars().all()
    assert len(claims) == 1                    # NO duplicate claim
    assert claims[0].id == first_claim         # same claim recovered
    job = db_session.get(IngestionJob, job_id)
    assert job.status == "done" and job.claim_id == first_claim


def _intakes_for(db_session, job_id):
    return db_session.execute(
        select(DocumentIntake).where(DocumentIntake.ingestion_job_id == job_id)
    ).scalars().all()


def test_reclaimed_all_diverted_job_does_not_duplicate_intake(
    client, db_session, fake_ocr, fake_segmenter, tmp_path
):
    """F3 (B3 bug class, intake edition): an ALL-diverted job builds no claim, so the
    claim-based idempotency check can't detect its prior completion. A reclaim must NOT
    rebuild and double-record the vendor bills."""
    _enable_split(db_session)                 # each page → its own diverted page
    fake_ocr.extraction = Extraction(
        vendor="Acme", total_amount=Decimal("100"),
        document_type="vendor_invoice", type_confidence=Decimal("0.95"),
    )
    job_id = _enqueue_via_http(client, 4)
    provs = _providers(fake_ocr, fake_segmenter, tmp_path)

    assert worker.process_one(db_session, provs) == job_id
    db_session.expire_all()
    job = db_session.get(IngestionJob, job_id)
    assert job.status == "done" and job.claim_id is None      # all diverted, no claim
    assert len(_intakes_for(db_session, job_id)) == 4         # 4 bills captured
    assert db_session.execute(select(Claim)).scalars().all() == []

    # Simulate a crash before completion: job back to running, heartbeat stale.
    db_session.execute(
        text("UPDATE ingestion_job SET status='running', "
             "heartbeat_at = now() - interval '10 minutes' WHERE id = :i"),
        {"i": str(job_id)},
    )
    db_session.commit()

    assert worker.process_one(db_session, provs) == job_id    # recovered, not rebuilt
    db_session.expire_all()
    assert len(_intakes_for(db_session, job_id)) == 4         # NOT duplicated
    assert db_session.get(IngestionJob, job_id).status == "done"


def test_mixed_async_upload_files_receipts_and_diverts_bills(
    client, db_session, fake_segmenter, tmp_path
):
    """F9: a MIXED async upload builds the claim from its receipt pages while diverting
    its vendor-bill pages to the holding queue — both in the same job."""
    import threading

    _enable_split(db_session)

    class _MixedOcr:
        def __init__(self):
            self._lock = threading.Lock()
            self._n = 0

        def extract(self, image_bytes, media_type):
            with self._lock:
                i = self._n
                self._n += 1
            if i % 2 == 0:
                return Extraction(
                    vendor="Bill Co", total_amount=Decimal("100"),
                    document_type="vendor_invoice", type_confidence=Decimal("0.95"),
                )
            return Extraction(
                vendor="Cafe", total_amount=Decimal("20"),
                document_type="expense_receipt", type_confidence=Decimal("0.9"),
            )

    job_id = _enqueue_via_http(client, 4)                 # 4 pages
    assert worker.process_one(db_session, _providers(_MixedOcr(), fake_segmenter, tmp_path)) == job_id
    db_session.expire_all()

    job = db_session.get(IngestionJob, job_id)
    assert job.status == "done" and job.claim_id is not None
    lines = db_session.execute(
        select(ClaimLine).where(ClaimLine.claim_id == job.claim_id)
    ).scalars().all()
    assert len(lines) == 2                                # 2 receipts became claim lines
    assert len(_intakes_for(db_session, job_id)) == 2     # 2 bills diverted


def test_intake_job_sha_unique_blocks_redivert_but_allows_inline(client, db_session):
    """The partial UNIQUE(ingestion_job_id, image_sha256) blocks a re-divert of the same
    page for one job, while inline captures (NULL job) stay unconstrained (F3)."""
    from sqlalchemy.exc import IntegrityError

    ids = db_session.info["principal"]
    jid = uuid.uuid4()

    def _row(job, sha):
        return DocumentIntake(
            firm_id=ids["firm"], client_id=ids["client"], document_type="vendor_invoice",
            routed_to="ap_holding", ingestion_job_id=job, image_sha256=sha, type_signals=[],
        )

    db_session.add(_row(jid, "sha-1")); db_session.flush()
    db_session.add(_row(jid, "sha-1"))                        # same job + sha → blocked
    try:
        db_session.flush()
        raise AssertionError("expected the UNIQUE to block a re-divert")
    except IntegrityError:
        db_session.rollback()

    # NULL job (inline) → the partial index doesn't apply; two identical shas are fine.
    db_session.add(_row(None, "sha-x")); db_session.flush()
    db_session.add(_row(None, "sha-x")); db_session.flush()   # no error


def test_staged_files_are_cleaned_up_after_success(client, db_session, fake_ocr, fake_segmenter, tmp_path):
    job_id = _enqueue_via_http(client, 4)
    staging = tmp_path / "staging" / str(job_id)
    assert staging.exists()               # staged during enqueue
    worker.process_one(db_session, _providers(fake_ocr, fake_segmenter, tmp_path))
    assert not staging.exists()           # removed once the claim is built


# --- Phase 3: OCR cache, janitor, inbox chip, status payload ----------------
def test_ocr_cache_serves_repeat_reads(tmp_path):
    class CountingOcr:
        def __init__(self):
            self.n = 0

        def extract(self, b, mt):
            self.n += 1
            return Extraction(vendor="ACME")

    ocr = CountingOcr()
    receipts = [{"item": None, "bytes": b"IMAGE-BYTES-1", "media_type": "image/png"}]
    cache = tmp_path / "ocr_cache"

    r1 = ingestion.prefetch_ocr(ocr, receipts, cache_dir=cache, model="m1")
    r2 = ingestion.prefetch_ocr(ocr, receipts, cache_dir=cache, model="m1")
    assert ocr.n == 1                       # 2nd read served from disk cache
    assert r1[0].vendor == "ACME" and r2[0].vendor == "ACME"
    # A different model key is a cache miss (re-reads).
    ingestion.prefetch_ocr(ocr, receipts, cache_dir=cache, model="m2")
    assert ocr.n == 2


def _make_job(db_session, status: str) -> uuid.UUID:
    ids = db_session.info["principal"]
    jid = uuid.uuid4()
    db_session.add(IngestionJob(
        id=jid, firm_id=ids["firm"], client_id=ids["client"], status=status, payload={},
    ))
    db_session.flush()
    return jid


def _age_dir(path, hours: float):
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


def test_staging_janitor_removes_only_old_terminal_orphans(db_session, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()

    done_id = _make_job(db_session, "failed")     # terminal → sweepable
    running_id = _make_job(db_session, "running")  # active → keep its files

    for jid in (done_id, running_id):
        d = staging / str(jid)
        d.mkdir()
        (d / "0000.bin").write_bytes(b"x")
        _age_dir(d, hours=48)                      # older than the 24h TTL

    # A fresh terminal job's dir is too new to sweep.
    fresh_id = _make_job(db_session, "failed")
    fresh_dir = staging / str(fresh_id)
    fresh_dir.mkdir()

    removed = worker.sweep_staging(db_session, tmp_path, ttl_hours=24)
    assert removed == 1
    assert not (staging / str(done_id)).exists()   # old + terminal → gone
    assert (staging / str(running_id)).exists()     # old but active → kept
    assert fresh_dir.exists()                        # terminal but too new → kept


def test_inbox_shows_processing_banner(client, db_session):
    job_id = _enqueue_via_http(client, 6)
    page = client.get("/claims")
    assert page.status_code == 200
    assert "still being read" in page.text
    assert str(job_id)[:8] in page.text            # links to the progress page


def test_job_status_dict_maps_states():
    from eclaim.web.routes import _job_status_dict

    assert _job_status_dict(None) == {"state": "unknown"}

    class J:
        pass

    j = J()
    j.status, j.done_units, j.total_units, j.error, j.claim_id = "running", 3, 10, None, None
    d = _job_status_dict(j)
    assert d["state"] == "running" and d["done"] == 3 and d["total"] == 10 and d["redirect"] is None

    j.status, j.claim_id = "done", uuid.uuid4()
    assert _job_status_dict(j)["redirect"] == f"/claims/{j.claim_id}/review"
