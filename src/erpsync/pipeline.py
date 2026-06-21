"""Pipeline orchestration: S1 → S3 → S4 → calc → S8 → S6.

``run_import`` takes a listing file and the per-client configuration, and
returns a :class:`PipelineResult` carrying the validation report, the staged
emission entries, the cross-channel duplicate findings, and the deterministic
batch hash.

Whole-batch commit policy (a baked-in pass-1 default):

* any ``REJECTED`` (malformed) row  -> nothing commits; report returned for fixing
* ``WARNING`` rows                   -> commit, staged spend-based / DQ-flagged
* ``DUPLICATE`` rows (idempotency)   -> skipped
* cross-channel duplicate hits       -> held back from commit, reported (FR-S8)

The batch hash (FR-S6) is computed over exactly the committed entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .dedup.crosschannel import OwnershipMatrix, screen
from .domain.enums import BatchStatus, DataQuality, QuantityBasis, RowStatus, Scope
from .domain.models import (
    DuplicateHit,
    EmissionEntry,
    RowOutcome,
    RuleSet,
    SourceRecord,
    ValidationReport,
)
from .emissions.calculator import compute
from .emissions.factors import FactorSet
from .ingest.batch import file_sha256, source_snapshot_hash
from .ingest.column_preset import ColumnPreset
from .ingest.reader import read_listing
from .ingest.validate import ParsedRow, validate_rows
from .persistence.store import Store
from .quantity.resolver import resolve
from .release.gate import ReleaseGate
from .rules.engine import match_rule


class StagingSink(Protocol):
    """The reviewable per-line sink (e.g. ``PgStagingStore`` → ``erpsync_entry``).

    Optional and storage-agnostic: the pipeline hands it EVERY accepted line tagged
    with a review status (``clean`` / ``held`` / ``flagged``) and the sink decides
    where that lands. Kept a Protocol so the pure-Python pipeline never imports the
    Postgres / e-Claim DB stack.
    """

    def stage(self, rows: list[tuple[EmissionEntry, str]]) -> int: ...


@dataclass(frozen=True)
class PipelineConfig:
    client_id: str
    preset: ColumnPreset
    ruleset: RuleSet
    factors: FactorSet
    ownership: OwnershipMatrix
    eclaim_doc_numbers: set[str] = field(default_factory=set)


@dataclass
class PipelineResult:
    report: ValidationReport
    batch_status: BatchStatus
    entries: list[EmissionEntry]            # committed entries (empty if rejected)
    duplicate_hits: list[DuplicateHit]
    batch_hash: str | None
    committed_count: int


def _build_entry(parsed: ParsedRow, config: PipelineConfig) -> tuple[EmissionEntry, RowStatus]:
    """Map → resolve → compute one accepted row into an EmissionEntry.

    Returns the entry plus the refined row status (CLEAN or WARNING).
    """
    record: SourceRecord = parsed.record
    src_hash = source_snapshot_hash(record)
    match = match_rule(record, config.ruleset)

    if match is None:
        # Mappable file row, but no carbon rule matched — keep it visible for
        # the review queue rather than dropping it. Zero until mapped.
        entry = EmissionEntry(
            line_key=record.line_key,
            doc_entry=record.doc_entry,
            line_num=record.line_num,
            doc_number=record.doc_number,
            category="UNMAPPED",
            scope=Scope.S3_OTHER,
            basis=QuantityBasis.SPEND,
            data_quality=DataQuality.FLAGGED,
            quantity=None,
            uom=None,
            amount=record.amount,
            factor_ref="",
            factor_value=_zero(),
            factor_version=config.factors.version,
            rule_id="",
            rule_version=config.ruleset.version,
            tco2e=_zero(),
            source_hash=src_hash,
            notes=("no mapping rule matched — needs a rule",),
        )
        return entry, RowStatus.WARNING

    rule = match.rule
    resolution = resolve(record, rule, config.factors)
    emission = compute(resolution, config.factors)

    entry = EmissionEntry(
        line_key=record.line_key,
        doc_entry=record.doc_entry,
        line_num=record.line_num,
        doc_number=record.doc_number,
        category=rule.category,
        scope=rule.scope,
        basis=resolution.basis,
        data_quality=resolution.data_quality,
        quantity=resolution.quantity,
        uom=resolution.uom,
        amount=resolution.amount,
        factor_ref=emission.factor_ref or "",
        factor_value=emission.factor_value,
        factor_version=emission.factor_version,
        rule_id=rule.rule_id,
        rule_version=match.rule_version,
        tco2e=emission.tco2e,
        source_hash=src_hash,
        notes=resolution.notes,
    )
    status = (
        RowStatus.CLEAN
        if resolution.data_quality is DataQuality.MEASURED
        else RowStatus.WARNING
    )
    return entry, status


def run_import(
    file_path: str | Path,
    config: PipelineConfig,
    store: Store,
    gate: ReleaseGate | None = None,
    *,
    staging: StagingSink | None = None,
) -> PipelineResult:
    gate = gate or ReleaseGate()
    path = Path(file_path)

    raw_rows = read_listing(path)
    fsha = file_sha256(path)
    validation = validate_rows(
        raw_rows,
        config.preset,
        config.client_id,
        seen_keys=store.known_keys(config.client_id),
    )

    outcomes: list[RowOutcome] = list(validation.outcomes)  # rejected + duplicate

    # If the file is malformed, stop before any mapping or commit.
    if any(o.status is RowStatus.REJECTED for o in outcomes):
        report = ValidationReport(
            file_name=path.name,
            file_sha256=fsha,
            total_rows=len(raw_rows),
            outcomes=tuple(_sorted(outcomes)),
        )
        return PipelineResult(
            report=report,
            batch_status=BatchStatus.REJECTED,
            entries=[],
            duplicate_hits=[],
            batch_hash=None,
            committed_count=0,
        )

    # Map every accepted row.
    staged: list[EmissionEntry] = []
    status_by_key: dict[tuple, RowStatus] = {}
    index_by_key: dict[tuple, int] = {}
    for parsed in validation.accepted:
        entry, status = _build_entry(parsed, config)
        staged.append(entry)
        status_by_key[entry.line_key] = status
        index_by_key[entry.line_key] = parsed.row_index

    # FR-S8 cross-channel screen; hits are held back from the commit.
    hits = screen(staged, config.ownership, eclaim_doc_numbers=config.eclaim_doc_numbers)
    hit_keys = {h.line_key for h in hits}

    committed = [e for e in staged if e.line_key not in hit_keys]

    # Build the per-row outcomes for accepted rows (refined statuses).
    for entry in staged:
        if entry.line_key in hit_keys:
            hit = next(h for h in hits if h.line_key == entry.line_key)
            outcomes.append(
                RowOutcome(
                    row_index=index_by_key[entry.line_key],
                    status=RowStatus.DUPLICATE,
                    line_key=entry.line_key,
                    messages=(hit.reason,),
                )
            )
        else:
            outcomes.append(
                RowOutcome(
                    row_index=index_by_key[entry.line_key],
                    status=status_by_key[entry.line_key],
                    line_key=entry.line_key,
                    messages=entry.notes,
                )
            )

    report = ValidationReport(
        file_name=path.name,
        file_sha256=fsha,
        total_rows=len(raw_rows),
        outcomes=tuple(_sorted(outcomes)),
    )

    added = store.commit(committed)
    digest = gate.compute_hash(committed) if committed else None

    # Stage EVERY accepted line into the reviewable sink (erpsync_entry), tagged
    # with its review status — held cross-channel hits included, so they survive
    # for review instead of being merely reported. Idempotency is the sink's job.
    if staging is not None:
        staging.stage(
            [
                (entry, _stage_status(entry, hit_keys, status_by_key))
                for entry in staged
            ]
        )

    return PipelineResult(
        report=report,
        batch_status=BatchStatus.STAGED,
        entries=committed,
        duplicate_hits=hits,
        batch_hash=digest,
        committed_count=added,
    )


def _stage_status(entry, hit_keys, status_by_key) -> str:
    """The ``erpsync_entry.status`` for one accepted line.

    A cross-channel hit is ``held`` regardless of its own data quality; otherwise
    a measured/mapped row is ``clean`` and a WARNING row (unmapped / spend-based /
    DQ-flagged) is ``flagged``. (``released`` is set later, at projection time.)
    """
    if entry.line_key in hit_keys:
        return "held"
    return "clean" if status_by_key[entry.line_key] is RowStatus.CLEAN else "flagged"


def _sorted(outcomes: list[RowOutcome]) -> list[RowOutcome]:
    return sorted(outcomes, key=lambda o: o.row_index)


def _zero():
    from decimal import Decimal

    return Decimal("0.000000")
