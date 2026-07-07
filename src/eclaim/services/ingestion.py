"""Capture ingestion pipeline — shared by the synchronous (small upload) and the
asynchronous (large upload) capture paths.

A capture turns a batch of uploaded files (images, a ZIP of receipts, a possibly
multi-invoice PDF) plus any mileage trips into ONE ``in_review`` claim whose lines
are the individual receipts/trips. The slow part is reading each receipt with the
vision model; a 30-invoice PDF is dozens of calls. So:

* :func:`build_claim` is the pure pipeline: flatten the uploads → read every
  server-OCR receipt CONCURRENTLY → then create the claim, add its lines and submit
  in ONE transaction (atomic: a complete claim or nothing — no partial rows).
* The synchronous route calls it directly for a small upload (instant).
* For a large upload the route stages the files and enqueues an ``ingestion_job``;
  the in-process worker (:mod:`eclaim.ingest.worker`) reads the staged files and
  calls the same :func:`build_claim`, reporting progress on the job row.

Because the claim is only built in the final atomic transaction, a crash mid-read
never leaves a half-built claim — the job is simply re-run from scratch (idempotent
by reconstruction), which is why the worker needs no per-line dedup.
"""

from __future__ import annotations

import io
import logging
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import sessionmaker

from ..config import get_settings
from ..db.models import Event
from ..ocr.base import Extraction, OcrError, OcrProvider
from ..ocr.segment import PageSegmenter, one_per_page
from ..tenancy import set_tenant_context
from . import intake as intake_service
from . import routing
from .claims import CLAIM_TYPES, DATED_CLAIM_TYPES, ClaimError, ClaimService, Repos
from .documents import is_pdf, normalize_image, render_pdf_pages, stitch_pages

# Sentinel the Submit page posts in ``event_id`` to mean "create a new trip from
# the inline fields" rather than attach an existing event.
NEW_EVENT = "__new__"

# HEIC/HEIF (iPhone photos) are accepted here and transcoded to JPEG on the way in
# (see documents.normalize_image) — downstream OCR/storage/viewer never see HEIC.
SUPPORTED_MEDIA = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}

# A ZIP upload is a batch of receipts (one image per line). Attacker-controlled, so
# expansion is bounded against zip bombs (entry count, per-entry + total size).
_ZIP_MEDIA = {"application/zip", "application/x-zip-compressed", "multipart/x-zip"}
_ZIP_MAX_ENTRIES = 50
_ZIP_MAX_TOTAL_BYTES = 100 * 1024 * 1024
_ZIP_MAX_ENTRY_BYTES = 25 * 1024 * 1024
_EXT_MEDIA = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".heic": "image/heic", ".heif": "image/heif",
}

# Bound the concurrent server-OCR calls per capture (a big PDF is read page by page).
OCR_MAX_CONCURRENCY = 8

_service = ClaimService()


# --------------------------------------------------------------------------- #
# Small pure helpers (moved here so both the web route and the worker share them)
# --------------------------------------------------------------------------- #
def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value.strip() else None
    except ValueError:
        return None


def is_zip(name: str, media_type: str) -> bool:
    return media_type in _ZIP_MEDIA or (name or "").lower().endswith(".zip")


def expand_zip(name: str, data: bytes) -> tuple[list[dict], list[str]]:
    """Expand a ZIP of receipt images into per-entry inputs, bounded against zip
    bombs. Directories, hidden files, macOS resource forks and non-image entries are
    skipped; nested paths are flattened to the base name. Returns (inputs, errors)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return [], [f"{name}: not a readable ZIP archive"]
    inputs: list[dict] = []
    errors: list[str] = []
    total = 0
    kept = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        base = info.filename.rsplit("/", 1)[-1]
        if not base or base.startswith(".") or "__MACOSX" in info.filename:
            continue
        ext = ("." + base.rsplit(".", 1)[-1].lower()) if "." in base else ""
        media_type = _EXT_MEDIA.get(ext)
        if media_type is None:
            continue
        if info.file_size > _ZIP_MAX_ENTRY_BYTES:
            errors.append(f"{name} › {base}: image too large — skipped")
            continue
        if kept >= _ZIP_MAX_ENTRIES:
            errors.append(f"{name}: more than {_ZIP_MAX_ENTRIES} images — the rest were skipped")
            break
        if total + info.file_size > _ZIP_MAX_TOTAL_BYTES:
            errors.append(f"{name}: archive exceeds the size limit — remaining images skipped")
            break
        with zf.open(info) as fh:
            raw = fh.read(_ZIP_MAX_ENTRY_BYTES + 1)
        if len(raw) > _ZIP_MAX_ENTRY_BYTES:
            errors.append(f"{name} › {base}: image too large — skipped")
            continue
        total += len(raw)
        kept += 1
        inputs.append({"name": base, "media_type": media_type, "bytes": raw, "item": None})
    if not inputs and not errors:
        errors.append(f"{name}: no receipt images found in the ZIP")
    return inputs, errors


def item_has_data(item) -> bool:
    """True only if a per-file ``items`` entry actually carries read data (else the
    server OCRs the image itself)."""
    if not isinstance(item, dict):
        return False
    if item.get("category_id") or (item.get("expense_type") or "other") != "other":
        return True
    return any(item.get(k) for k in ("vendor", "doc_no", "date", "total_amount", "quantity", "unit"))


def extraction_from_item(item: dict) -> Extraction:
    """Build an Extraction from a per-file ``items`` entry (fields read client-side).

    Carries the classifier verdict (``document_type`` / ``type_confidence`` / signals /
    ``po_ref``) so a page pre-read via ``/capture/extract`` routes on it — a vendor bill
    dropped through the normal capture UI is diverted, not silently filed as an expense
    (F2). A purely-manual entry has no ``document_type`` → defaults to expense_receipt →
    e-Claim, unchanged."""
    return Extraction(
        vendor=item.get("vendor") or None,
        doc_no=item.get("doc_no") or None,
        date=item.get("date") or None,
        total_amount=Decimal(item["total_amount"]) if item.get("total_amount") else None,
        expense_type=item.get("expense_type") or "other",
        quantity=Decimal(item["quantity"]) if item.get("quantity") else None,
        unit=item.get("unit") or None,
        boxes=item.get("boxes") or None,
        document_type=item.get("document_type") or "expense_receipt",
        type_confidence=(
            Decimal(str(item["type_confidence"])) if item.get("type_confidence") not in (None, "") else None
        ),
        type_signals=item.get("type_signals") or [],
        po_ref=item.get("po_ref") or None,
    )


class _FormOcr:
    """An OcrProvider that just returns a pre-built Extraction (manual entry, or an
    already-fetched OCR result) — so ClaimService.add_line does no network call."""

    def __init__(self, extraction: Extraction) -> None:
        self._extraction = extraction

    def extract(self, image_bytes: bytes, media_type: str) -> Extraction:
        return self._extraction


def resolve_route(directions, origin: str, destination: str, wps: list[str], route_index: int):
    """Authoritatively compute the chosen route. Returns ``(chosen, shortest_km)``:
    ``shortest_km`` is the SHORTEST option's distance (the reimbursement baseline a
    longer chosen route is flagged against). An out-of-range index falls back to 0."""
    options = directions.routes(origin, destination, wps)
    idx = route_index if 0 <= route_index < len(options) else 0
    return options[idx], min(o.distance_km for o in options)


def _ocr_cache_path(cache_dir: Path, image_bytes: bytes, model: str) -> Path:
    import hashlib

    return cache_dir / f"{hashlib.sha256(image_bytes).hexdigest()}-{model}.json"


def _ocr_cache_get(cache_dir: Path | None, image_bytes: bytes, model: str) -> Extraction | None:
    if cache_dir is None:
        return None
    p = _ocr_cache_path(cache_dir, image_bytes, model)
    if not p.exists():
        return None
    try:
        return Extraction.model_validate_json(p.read_text())
    except Exception:
        return None


def _ocr_cache_set(cache_dir: Path | None, image_bytes: bytes, model: str, ex: Extraction) -> None:
    if cache_dir is None:
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _ocr_cache_path(cache_dir, image_bytes, model).write_text(ex.model_dump_json())
    except OSError:
        pass


def prefetch_ocr(
    ocr: OcrProvider,
    receipts: list[dict],
    on_progress: Callable[[int], None] | None = None,
    cache_dir: Path | None = None,
    model: str | None = None,
) -> dict[int, object]:
    """Read (concurrently) every receipt that needs SERVER OCR — one with no
    client-provided item. Returns ``{index: Extraction | Exception}``; a failed read
    is stored, not raised, so one bad page never sinks the batch. ``on_progress`` is
    called with the running count of completed reads (for a progress bar).

    A disk cache keyed by sha256(image)+model means a reclaimed/retried job (or a
    genuinely duplicate image) is served from cache instead of paying for OCR again —
    the atomic-rebuild retry model would otherwise re-read every page."""
    if model is None:
        model = get_settings().ocr_model
    jobs = [(i, r) for i, r in enumerate(receipts) if not item_has_data(r["item"])]
    results: dict[int, object] = {}
    if not jobs:
        return results

    to_read: list[tuple[int, dict]] = []
    for i, r in jobs:
        cached = _ocr_cache_get(cache_dir, r["bytes"], model)
        if cached is not None:
            results[i] = cached
        else:
            to_read.append((i, r))
    done = len(results)
    if on_progress and done:
        on_progress(done)

    def _read(r):
        try:
            return ocr.extract(r["bytes"], r["media_type"])
        except Exception as exc:   # OcrError per contract; guard anything else too
            return exc

    if to_read:
        by_idx = {i: r for i, r in to_read}
        with ThreadPoolExecutor(max_workers=min(OCR_MAX_CONCURRENCY, len(to_read))) as pool:
            futs = {pool.submit(_read, r): i for i, r in to_read}
            for fut in as_completed(futs):
                i = futs[fut]
                out = fut.result()
                results[i] = out
                if not isinstance(out, Exception):
                    _ocr_cache_set(cache_dir, by_idx[i]["bytes"], model, out)
                done += 1
                if on_progress:
                    on_progress(done)
    return results


# --------------------------------------------------------------------------- #
# Flatten uploads → receipt inputs
# --------------------------------------------------------------------------- #
def flatten_receipts(
    staged: list[dict], item_list: list, segmenter: PageSegmenter, split_docs: bool
) -> tuple[list[dict], list[str]]:
    """Turn uploaded parts into individual receipt inputs. An image passes through
    with its client-read ``item`` (aligned to the upload order); a ZIP expands into
    its images; a PDF renders to pages — split into one line per invoice (segmented)
    when ``split_docs`` is on, else kept whole as one stitched line. Mirrors the
    original inline capture logic. ``staged`` entries are ``{name, media_type, bytes}``."""
    receipts: list[dict] = []
    errors: list[str] = []
    for idx, f in enumerate(staged):
        name = f["name"]
        media_type = f["media_type"]
        raw = f["bytes"]
        if is_zip(name, media_type):
            zin, zerr = expand_zip(name, raw)
            receipts.extend(zin)
            errors.extend(zerr)
        elif is_pdf(name, media_type):
            try:
                pages = render_pdf_pages(raw)
            except ValueError as exc:
                errors.append(f"{name}: {exc}")
                pages = None
            if pages is None:
                pass
            elif not pages:
                errors.append(f"{name}: no readable pages in the PDF")
            elif split_docs:
                groups = segmenter.segment(pages) if len(pages) > 1 else one_per_page(len(pages))
                for grp in groups:
                    if len(grp) == 1:
                        receipts.append({"name": f"{name} (p{grp[0] + 1})", "media_type": "image/png",
                                         "bytes": pages[grp[0]], "item": None})
                    else:
                        page_bytes = [pages[i] for i in grp]
                        receipts.append({
                            "name": f"{name} (pp{grp[0] + 1}-{grp[-1] + 1})",
                            "media_type": "image/jpeg", "bytes": stitch_pages(page_bytes),
                            "item": None, "page_images": page_bytes,
                        })
            else:
                receipts.append({"name": name, "media_type": "image/jpeg",
                                 "bytes": stitch_pages(pages), "item": None})
        elif media_type in SUPPORTED_MEDIA:
            item = item_list[idx] if idx < len(item_list) else None
            receipts.append({"name": name, "media_type": media_type, "bytes": raw, "item": item})
        else:
            errors.append(f"{name}: unsupported file type")
    # Transcode any HEIC/HEIF receipt (direct upload OR a ZIP entry) to JPEG here —
    # BEFORE the concurrent OCR prefetch and the image store, both of which run on
    # these bytes and neither of which reads HEIC. A receipt whose HEIC won't decode
    # is dropped with a per-file error rather than failing the whole upload.
    normalized: list[dict] = []
    for r in receipts:
        try:
            r["bytes"], r["media_type"] = normalize_image(r["bytes"], r["media_type"], name=r["name"])
        except ValueError as exc:
            errors.append(f"{r['name']}: {exc}")
            continue
        normalized.append(r)
    return normalized, errors


# --------------------------------------------------------------------------- #
# Providers bundle + result
# --------------------------------------------------------------------------- #
@dataclass
class Providers:
    """Everything build_claim reads the world through. The web route passes its
    request-injected providers (fakes under test); the worker builds the real ones."""

    ocr: OcrProvider
    segmenter: PageSegmenter
    image_dir: Path
    mileage_rate: Decimal
    directions: object   # GoogleDirectionsProvider (only touched if there are trips)


@dataclass
class IngestResult:
    claim_id: uuid.UUID | None = None
    added: int = 0
    errors: list[str] = field(default_factory=list)
    # A header/validation failure means no claim was created at all (distinct from a
    # batch where the header was fine but every receipt failed to read).
    header_error: str | None = None
    # Pages the classifier routed OFF the e-Claim path into the intake holding queue
    # (vendor bills / delivery orders / low-confidence) instead of onto this claim (C1).
    diverted: int = 0
    diverted_ids: list[uuid.UUID] = field(default_factory=list)


def _resolve_header(header: dict) -> dict:
    """Validate + normalise the claim header BEFORE the slow read phase, so an
    obvious mistake fails fast. Returns resolved fields; raises ClaimError on a bad
    header. Mirrors ClaimService.start_claim + the inline-event checks."""
    claim_type = (header.get("claim_type") or "general").strip() or "general"
    if claim_type not in CLAIM_TYPES:
        raise ClaimError(f"unknown claim type {claim_type!r}")
    event_id = (header.get("event_id") or "").strip()
    if event_id == NEW_EVENT:
        title = (header.get("new_event_title") or "").strip()
        sd = parse_date(header.get("new_event_start") or "")
        ed = parse_date(header.get("new_event_end") or "")
        if not title:
            raise ClaimError("a new trip needs a title")
        if sd is None or ed is None:
            raise ClaimError("a new trip needs a start and end date")
        if ed < sd:
            raise ClaimError("the end date is before the start date")
        # A brand-new trip is never 'general'; honour a specific pick, else travel.
        ctype = claim_type if claim_type != "general" else "travel"
        return {"mode": "new_event", "ctype": ctype, "sd": sd, "ed": ed}
    ev_uuid = uuid.UUID(event_id) if event_id else None
    sd = parse_date(header.get("start_date") or "")
    ed = parse_date(header.get("end_date") or "")
    if ev_uuid is None and claim_type in DATED_CLAIM_TYPES and not (sd and ed):
        raise ClaimError(
            f"a {claim_type.replace('_', ' ')} claim needs a start and end date "
            "(or attach it to an event)"
        )
    if sd and ed and ed < sd:
        raise ClaimError("the end date is before the start date")
    return {"mode": "existing", "ctype": claim_type, "sd": sd, "ed": ed, "event_id": ev_uuid}


def build_claim(
    repos: Repos,
    providers: Providers,
    *,
    firm_id: uuid.UUID,
    client_id: uuid.UUID,
    created_by_user_id: uuid.UUID | None,
    allowed_client_ids,
    header: dict,
    staged: list[dict],
    item_list: list,
    mileage_specs: list,
    split_docs: bool,
    on_progress: Callable[[int, int], None] | None = None,
    ingestion_job_id: uuid.UUID | None = None,
    commit: bool = True,
) -> IngestResult:
    """The full capture pipeline. Reads every receipt concurrently, then creates the
    claim + lines + submit in one atomic transaction. Assumes the tenant context is
    already set on ``repos.session``. ``on_progress(done, total)`` is optional.

    ``ingestion_job_id`` keys the built claim to its async job (UNIQUE), so a
    re-claimed job can never build a duplicate (B3). ``commit=False`` leaves the
    successful claim flushed-but-uncommitted so the caller (the worker) can flip
    the job to ``done`` in the SAME transaction — one atomic commit for claim +
    job completion, closing the crash window entirely."""
    from ..tenancy import set_tenant_context

    def _reset_ctx():
        set_tenant_context(repos.session, firm_id, allowed_client_ids)

    # 1. Header validation (fast, no DB) — fail before the slow read phase.
    try:
        resolved = _resolve_header(header)
    except (ClaimError, ValueError) as exc:
        return IngestResult(header_error=str(exc))

    # 2. Flatten uploads (renders + segments a PDF — can be slow) then read every
    #    server-OCR receipt concurrently. No claim rows are written yet.
    receipts, errors = flatten_receipts(staged, item_list, providers.segmenter, split_docs)
    total = len(receipts) + len(mileage_specs)
    if on_progress:
        on_progress(0, total)
    ocr_results = prefetch_ocr(
        providers.ocr, receipts,
        on_progress=(lambda d: on_progress(d, total)) if on_progress else None,
        cache_dir=providers.image_dir / "ocr_cache",
    )

    # 3. Build the claim atomically. Any total failure rolls the whole thing back so
    #    no empty/partial claim is ever persisted.
    added = 0
    diverted: list[uuid.UUID] = []
    actor = _capture_actor(created_by_user_id, header)
    try:
        # 3a. Classify every receipt first (pure — no DB writes). The router (C1) sends
        #     an ``expense_receipt`` onto the claim and diverts anything else (vendor
        #     bill / delivery order / low-confidence) to the intake holding queue. A
        #     manually-keyed item is always an e-Claim line — the user entered it as an
        #     expense they are claiming, so it is never re-routed.
        classified: list[tuple] = []   # (r, extraction, cat_uuid, pay, decision)
        for i, r in enumerate(receipts):
            item = r["item"]
            try:
                if item_has_data(item):
                    # Pre-read by /capture/extract: carries the classifier verdict, so it
                    # is routed on document_type just like the server-OCR path — a vendor
                    # bill dropped through the normal UI diverts, not silently forced into
                    # e-Claim (F2). A purely-manual entry defaults to expense_receipt.
                    extraction = extraction_from_item(item)
                    cat_uuid = uuid.UUID(item["category_id"]) if item.get("category_id") else None
                    pay = item.get("payment_method") or "out_of_pocket"
                else:
                    pre = ocr_results.get(i)
                    if isinstance(pre, Exception):
                        raise pre if isinstance(pre, OcrError) else OcrError(str(pre))
                    extraction, cat_uuid, pay = pre, None, "out_of_pocket"
                decision = routing.route(extraction.document_type, extraction.type_confidence)
            except (OcrError, ClaimError, ValueError) as exc:
                errors.append(f"{r['name']}: {exc}")
                continue
            classified.append((r, extraction, cat_uuid, pay, decision))

        # 3b. Record diverted pages in THIS transaction but OUTSIDE the claim savepoint
        #     below, so they survive even when the upload has no e-Claim line and the
        #     empty claim is rolled back. Atomic with the whole capture, so an async
        #     worker retry rebuilds them cleanly rather than duplicating.
        for r, extraction, _cat, _pay, decision in classified:
            if decision.queue == routing.QUEUE_ECLAIM:
                continue
            try:
                with repos.session.begin_nested():
                    path, sha = _service._store_image(
                        providers.image_dir, r["bytes"], r["media_type"]
                    )
                    row, _ = intake_service.record_intake(
                        repos.session, firm_id=firm_id, client_id=client_id,
                        created_by_user_id=created_by_user_id, extraction=extraction,
                        provenance=intake_service.Provenance(
                            sha256=sha, path=path,
                            media_type=r["media_type"], name=r["name"],
                        ),
                        actor=actor,
                    )
                diverted.append(row.id)
            except (ClaimError, ValueError) as exc:
                errors.append(f"{r['name']}: {exc}")

        eclaim_receipts = [
            (r, e, c, p) for (r, e, c, p, d) in classified if d.queue == routing.QUEUE_ECLAIM
        ]

        # 3c. Build the claim inside its OWN savepoint, so an upload with no e-Claim line
        #     (every page diverted, or every line failed) rolls back the empty claim and
        #     any freshly-created trip WITHOUT discarding the diverted intakes above.
        claim = None
        if eclaim_receipts or mileage_specs:
            claim_sp = repos.session.begin_nested()
            try:
                if resolved["mode"] == "new_event":
                    ev = repos.events.add(Event(
                        firm_id=firm_id, client_id=client_id,
                        title=(header.get("new_event_title") or "").strip(),
                        event_type="travel", start_date=resolved["sd"], end_date=resolved["ed"],
                        organiser_user_id=created_by_user_id, status="active",
                    ))
                    event_id = ev.id
                    start_date = end_date = None   # claim inherits the trip's dates
                else:
                    event_id = resolved.get("event_id")
                    start_date, end_date = resolved["sd"], resolved["ed"]
                claim = _service.start_claim(
                    repos=repos, firm_id=firm_id, client_id=client_id,
                    title=(header.get("title") or "").strip() or None,
                    purpose=(header.get("purpose") or "").strip() or None,
                    remarks=(header.get("remarks") or "").strip() or None,
                    posting_date=parse_date(header.get("posting_date") or ""),
                    claim_type=resolved["ctype"], start_date=start_date, end_date=end_date,
                    event_id=event_id, created_by_user_id=created_by_user_id,
                )

                for r, extraction, cat_uuid, pay in eclaim_receipts:
                    try:
                        with repos.session.begin_nested():
                            _service.add_line(
                                repos=repos, claim=claim, image_bytes=r["bytes"],
                                media_type=r["media_type"], ocr=_FormOcr(extraction),
                                image_dir=providers.image_dir, category_id=cat_uuid,
                                payment_method=pay, page_images=r.get("page_images"),
                            )
                        added += 1
                    except (OcrError, ClaimError, ValueError) as exc:
                        errors.append(f"{r['name']}: {exc}")

                for spec in mileage_specs:
                    if not isinstance(spec, dict):
                        continue
                    origin = str(spec.get("origin") or "").strip()
                    destination = str(spec.get("destination") or "").strip()
                    sdate = str(spec.get("trip_date") or "").strip()
                    if not origin or not destination:
                        errors.append("mileage: a trip is missing From/To")
                        continue
                    if not sdate:
                        errors.append(f"mileage {origin} → {destination}: a trip date is required")
                        continue
                    wps = [w for w in (spec.get("waypoints") or []) if isinstance(w, str) and w.strip()]
                    try:
                        ridx = int(spec.get("route_index") or 0)
                    except (TypeError, ValueError):
                        ridx = 0
                    try:
                        with repos.session.begin_nested():
                            route, shortest_km = resolve_route(providers.directions, origin, destination, wps, ridx)
                            _service.add_mileage_line(
                                repos=repos, claim=claim, origin=origin, destination=destination,
                                waypoints=wps, route=route, date=sdate or None,
                                rate=providers.mileage_rate, shortest_km=shortest_km,
                            )
                        added += 1
                    except (ClaimError, ValueError) as exc:
                        errors.append(f"mileage {origin} → {destination}: {exc}")
                    except Exception as exc:   # MapError etc. — never abort the whole capture
                        errors.append(f"mileage {origin} → {destination}: {exc}")

                if added == 0:
                    claim_sp.rollback()        # empty claim → drop it (keep the diversions)
                    claim = None
                else:
                    _service.submit(repos=repos, claim=claim, actor=actor,
                                    line_count=added, attested=bool(header.get("attested")))
                    if ingestion_job_id is not None:
                        claim.ingestion_job_id = ingestion_job_id
                    claim_sp.commit()
            except Exception:
                if claim_sp.is_active:
                    claim_sp.rollback()
                raise

        # Nothing landed at all — no e-Claim line AND no diverted page → persist nothing.
        if added == 0 and not diverted:
            repos.session.rollback()
            _reset_ctx()
            return IngestResult(added=0, errors=errors)

        repos.session.flush()   # assign ids; make the job link durable-on-commit
        if commit:
            repos.session.commit()
        return IngestResult(
            claim_id=(claim.id if claim is not None else None),
            added=added, diverted=len(diverted), diverted_ids=diverted, errors=errors,
        )
    except Exception:
        repos.session.rollback()
        _reset_ctx()
        raise


def summarize_errors(errors: list[str], limit: int = 3) -> str:
    """Join the per-receipt errors for display, capped so a mass failure (e.g. every
    page hits the same OCR/billing error) doesn't spew dozens of identical lines."""
    if not errors:
        return ""
    head = " · ".join(errors[:limit])
    if len(errors) > limit:
        head += f" · (+{len(errors) - limit} more)"
    return head


def _capture_actor(user_id: uuid.UUID | None, header: dict) -> str:
    """Actor string for the submit audit — the keying user's email when the caller
    passed it in the header snapshot, else the user id, else 'system'.

    Falling through to 'system' means a claim (a money event) entered the ledger with
    NO human attributed to it — which should never happen on an authenticated capture.
    Warn loudly if it does, so an un-attributed audit row can't pass silently."""
    actor = header.get("actor") or (str(user_id) if user_id else None)
    if actor is None:
        logging.getLogger(__name__).warning(
            "capture actor fell back to 'system' — audit row will be un-attributed "
            "(no actor in header, no user_id). header keys=%s",
            sorted(header),
        )
        return "system"
    return actor


# --------------------------------------------------------------------------- #
# Async staging + enqueue (large uploads → ingestion_job for the worker)
# --------------------------------------------------------------------------- #
# A capture with more discrete receipts than this reads too slowly to do inside the
# request without the browser looking hung, so it goes async. Small ones stay inline
# (instant), which also keeps the existing synchronous behaviour + tests unchanged.
INLINE_MAX_UNITS = 3


def estimate_units(staged: list[dict]) -> int:
    """Cheaply estimate how many receipt lines an upload will yield WITHOUT the slow
    render/segment/OCR — images count 1, a ZIP by its image entries, a PDF by its
    page count. Used only to pick the inline vs async path."""
    total = 0
    for f in staged:
        name, media_type, raw = f["name"], f["media_type"], f["bytes"]
        if is_zip(name, media_type):
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
                total += sum(
                    1 for n in zf.namelist()
                    if _EXT_MEDIA.get("." + n.rsplit(".", 1)[-1].lower() if "." in n else "")
                    and not n.endswith("/") and "__MACOSX" not in n
                )
            except zipfile.BadZipFile:
                total += 1
        elif is_pdf(name, media_type):
            try:
                import pypdfium2 as pdfium

                doc = pdfium.PdfDocument(raw)
                total += len(doc)
                doc.close()
            except Exception:
                total += 1
        else:
            total += 1
    return total


def _staging_dir(image_dir: Path, job_id: uuid.UUID) -> Path:
    return image_dir / "staging" / str(job_id)


def stage_files(image_dir: Path, job_id: uuid.UUID, staged: list[dict]) -> list[dict]:
    """Persist raw uploads so the worker can read them after the request returns.
    Returns a manifest ``[{name, media_type, file}]`` (``file`` is the stored name)."""
    d = _staging_dir(image_dir, job_id)
    d.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, f in enumerate(staged):
        fname = f"{i:04d}.bin"
        (d / fname).write_bytes(f["bytes"])
        manifest.append({"name": f["name"], "media_type": f["media_type"], "file": fname})
    return manifest


def read_staged(image_dir: Path, job_id: uuid.UUID, manifest: list[dict]) -> list[dict]:
    """Load the staged files back into the ``{name, media_type, bytes}`` form flatten
    expects."""
    d = _staging_dir(image_dir, job_id)
    out = []
    for m in manifest:
        out.append({"name": m["name"], "media_type": m["media_type"],
                    "bytes": (d / m["file"]).read_bytes()})
    return out


def cleanup_staging(image_dir: Path, job_id: uuid.UUID) -> None:
    """Remove a finished job's staged files (best-effort)."""
    import shutil

    try:
        shutil.rmtree(_staging_dir(image_dir, job_id))
    except OSError:
        pass


def enqueue_job(
    repos: Repos,
    *,
    job_id: uuid.UUID,
    firm_id: uuid.UUID,
    client_id: uuid.UUID,
    created_by_user_id: uuid.UUID | None,
    allowed_client_ids,
    header: dict,
    item_list: list,
    mileage_specs: list,
    split_docs: bool,
    manifest: list[dict],
    total_estimate: int,
) -> "IngestionJob":
    """Insert a queued ingestion_job row carrying everything the worker needs. Runs
    under the request's tenant context (firm+client scoped), so RLS is satisfied."""
    from ..db.models import IngestionJob

    job = IngestionJob(
        id=job_id, firm_id=firm_id, client_id=client_id,
        created_by_user_id=created_by_user_id, status="queued",
        total_units=total_estimate, done_units=0,
        payload={
            "snapshot": {
                "firm_id": str(firm_id), "client_id": str(client_id),
                "created_by_user_id": str(created_by_user_id) if created_by_user_id else None,
                "allowed_client_ids": [str(c) for c in allowed_client_ids],
            },
            "header": header,
            "items": item_list,
            "mileage": mileage_specs,
            "split_docs": split_docs,
            "manifest": manifest,
        },
    )
    repos.session.add(job)
    repos.session.flush()
    return job
