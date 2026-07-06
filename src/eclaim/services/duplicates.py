"""Automatic duplicate detection (Appendix A, Layer 3).

The most common real abuse is the *same expense claimed twice*. On the review
screen we FLAG (never block) a line that looks like an expense already recorded
for this client — the approver decides. Two signals, per Appendix A:

* the same receipt image (``image_sha256``), or the same (vendor, amount, date)
  tuple, in another e-Claim; and
* the same invoice number + amount already in the ERP-Sync feed (double booking a
  corporate-card spend that was also claimed out of pocket).

Cross-channel matching is deliberately narrow: ``erpsync_entry`` carries no vendor
or date (it's a GL-line ingestion, not a receipt), so the only specific shared
keys are ``amount`` + ``doc_number`` — we require both, non-null, to avoid noise.
All queries are RLS-scoped and additionally filtered to the claim's client.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, or_, select

from ..db.models import Claim, ClaimLine, ErpsyncEntry


@dataclass(frozen=True)
class DuplicateMatch:
    channel: str          # "e-Claim" | "ERP-Sync"
    reference: str        # human-readable pointer to the matching record
    reason: str           # why it matched
    status: str | None = None


def _eclaim_matches(session, claim: Claim, line: ClaimLine) -> list[DuplicateMatch]:
    conds = []
    if line.image_sha256:
        conds.append(ClaimLine.image_sha256 == line.image_sha256)
    if line.vendor and line.total_amount is not None and line.doc_date:
        conds.append(
            and_(
                ClaimLine.vendor == line.vendor,
                ClaimLine.total_amount == line.total_amount,
                ClaimLine.doc_date == line.doc_date,
            )
        )
    if not conds:
        return []
    rows = session.execute(
        select(ClaimLine, Claim.claim_no)
        .join(Claim, ClaimLine.claim_id == Claim.id)
        .where(
            ClaimLine.client_id == claim.client_id,
            ClaimLine.claim_id != claim.id,
            Claim.status != "rejected",     # a rejected claim was never paid
            or_(*conds),
        )
    ).all()
    matches = []
    for other, claim_no in rows:
        same_image = bool(
            line.image_sha256 and other.image_sha256 == line.image_sha256
        )
        matches.append(
            DuplicateMatch(
                channel="e-Claim",
                reference=f"{claim_no or 'another claim'} · line {other.line_no}",
                reason="same receipt image" if same_image else "same vendor, amount & date",
            )
        )
    return matches


def _erpsync_matches(session, claim: Claim, line: ClaimLine) -> list[DuplicateMatch]:
    # Only the amount + invoice number are shared with the ERP feed; require both.
    if line.total_amount is None or not line.doc_no:
        return []
    rows = session.execute(
        select(ErpsyncEntry).where(
            ErpsyncEntry.client_id == claim.client_id,
            ErpsyncEntry.amount == line.total_amount,
            ErpsyncEntry.doc_number == line.doc_no,
        )
    ).scalars().all()
    return [
        DuplicateMatch(
            channel="ERP-Sync",
            reference=f"ERP doc {e.doc_number} · line {e.line_num}",
            reason="same invoice no & amount already in ERP-Sync",
            status=e.status,
        )
        for e in rows
    ]


def find_duplicates(repos, claim: Claim, lines: list[ClaimLine]) -> list[dict]:
    """Return one entry per flagged line: ``{line_no, line_id, matches}`` (matches is
    a non-empty list of :class:`DuplicateMatch`). Advisory only — a warning for the
    approver, never a hard block."""
    session = repos.session
    flags: list[dict] = []
    for ln in lines:
        matches = _eclaim_matches(session, claim, ln) + _erpsync_matches(session, claim, ln)
        if matches:
            flags.append({"line_no": ln.line_no, "line_id": ln.id, "matches": matches})
    return flags
