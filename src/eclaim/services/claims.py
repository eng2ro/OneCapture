"""Claim lifecycle service: upload → review/edit → approve → release → reverse.

Holds the domain logic for one e-Claim. Persistence goes through the
repositories; carbon maths through :mod:`core.carbon`; the release anchor and
audit chain through :mod:`core.release` / :mod:`core.audit`. The service never
commits — the caller (API route) owns the transaction, so each operation is
all-or-nothing.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from core.release import StubSink, StubTSA, canonical_hash

if TYPE_CHECKING:
    from ..auth.principal import Principal

from ..db.models import Claim, EmissionEntry, ReleaseBatch
from ..ocr.base import Extraction, OcrProvider
from ..repositories import (
    AuditRepository,
    ClaimRepository,
    FactorRepository,
    ReleaseRepository,
)
from .audit import record_event
from .classify import Classification, classify

SOURCE_TYPE = "eclaim"

_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class ClaimError(RuntimeError):
    """Base for claim-service errors (mapped to 4xx by the API)."""


class ClaimNotFound(ClaimError):
    pass


class IllegalTransition(ClaimError):
    """An operation not allowed from the claim's current status."""


class ClaimService:
    """Stateless: all state is in the repositories passed per call."""

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _store_image(image_dir: Path, image_bytes: bytes, media_type: str) -> tuple[str, str]:
        sha = hashlib.sha256(image_bytes).hexdigest()
        image_dir.mkdir(parents=True, exist_ok=True)
        path = image_dir / f"{sha}{_EXT.get(media_type, '.bin')}"
        if not path.exists():
            path.write_bytes(image_bytes)
        return str(path), sha

    @staticmethod
    def _apply_classification(claim: Claim, c: Classification) -> None:
        claim.scope = c.scope
        claim.factor_key = c.factor_key
        claim.factor_version = c.factor_version
        claim.basis = c.basis
        claim.tco2e = c.tco2e
        claim.data_quality = c.data_quality
        claim.quantity = c.quantity
        claim.unit = c.unit

    @staticmethod
    def _projection(claim: Claim) -> dict:
        """Carbon-relevant, hash-stable projection of a claim (spec §8)."""
        return {
            "claim_id": str(claim.id),
            "scope": claim.scope,
            "factor_key": claim.factor_key,
            "factor_version": claim.factor_version,
            "quantity": None if claim.quantity is None else format(claim.quantity, "f"),
            "tco2e": format(claim.tco2e, "f"),
        }

    # -- operations -------------------------------------------------------- #
    def upload(
        self,
        *,
        repos: "Repos",
        firm_id: uuid.UUID,
        client_id: uuid.UUID,
        image_bytes: bytes,
        media_type: str,
        ocr: OcrProvider,
        image_dir: Path,
        spend_factor: Decimal,
        actor: str,
        claimant_ref: str | None = None,
        submitted_by_claimant_id: uuid.UUID | None = None,
    ) -> Claim:
        """Store image → OCR → classify → insert claim (in_review) + 'submitted'.

        OCR failure raises (``OcrError``) before anything is persisted — no
        partial claim. This is the claimant-submission channel, so
        ``created_by_user_id`` stays null (no firm user keyed it); approval by a
        firm user therefore never trips the SoD self-approval rule.
        """
        extraction: Extraction = ocr.extract(image_bytes, media_type)
        image_path, image_sha = self._store_image(image_dir, image_bytes, media_type)
        result = classify(extraction, repos.factors, spend_factor)

        claim = Claim(
            firm_id=firm_id,
            client_id=client_id,
            source_channel="upload",
            claimant_ref=claimant_ref,
            submitted_by_claimant_id=submitted_by_claimant_id,
            vendor=extraction.vendor,
            doc_no=extraction.doc_no,
            doc_date=extraction.date,
            currency=extraction.currency,
            total_amount=extraction.total_amount,
            expense_type=extraction.expense_type,
            ocr_confidence=extraction.confidence,
            image_path=image_path,
            image_sha256=image_sha,
            status="in_review",
        )
        self._apply_classification(claim, result)
        repos.claims.add(claim)
        record_event(
            repos.audit,
            firm_id=firm_id,
            client_id=client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="submitted",
            actor=actor,
            detail={"image_sha256": image_sha, "expense_type": extraction.expense_type},
        )
        return claim

    def get(self, repos: "Repos", claim_id: uuid.UUID) -> Claim:
        claim = repos.claims.get(claim_id)
        if claim is None:
            raise ClaimNotFound(str(claim_id))
        return claim

    def edit(
        self,
        *,
        repos: "Repos",
        claim_id: uuid.UUID,
        fields: dict,
        spend_factor: Decimal,
        actor: str,
    ) -> Claim:
        """Edit extracted fields and re-classify. Forbidden once released."""
        claim = self.get(repos, claim_id)
        if claim.status == "released":
            raise IllegalTransition("a released claim is immutable")

        editable = {
            "vendor", "doc_no", "doc_date", "currency",
            "total_amount", "expense_type", "quantity", "unit",
        }
        for key, value in fields.items():
            if key in editable:
                setattr(claim, key, value)

        extraction = Extraction(
            vendor=claim.vendor,
            doc_no=claim.doc_no,
            date=claim.doc_date,
            currency=claim.currency,
            total_amount=claim.total_amount,
            expense_type=claim.expense_type or "other",
            quantity=claim.quantity,
            unit=claim.unit,
            confidence=claim.ocr_confidence,
        )
        self._apply_classification(claim, classify(extraction, repos.factors, spend_factor))
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="edited",
            actor=actor,
            detail={"fields": sorted(fields)},
        )
        return claim

    def approve(
        self,
        *,
        repos: "Repos",
        claim_id: uuid.UUID,
        actor: str,
        approver: "Principal | None" = None,
    ) -> Claim:
        """Approve an in-review claim. When an ``approver`` principal is given,
        the SoD/authority guard runs and ``approved_by_user_id`` is recorded."""
        claim = self.get(repos, claim_id)
        if claim.status != "in_review":
            raise IllegalTransition(f"cannot approve a claim in status {claim.status!r}")
        if approver is not None:
            from .sod import check_can_approve

            check_can_approve(claim, approver)
            claim.approved_by_user_id = approver.user_id
        claim.status = "approved"
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="approved",
            actor=actor,
        )
        return claim

    def release(self, *, repos: "Repos", claim_id: uuid.UUID, actor: str) -> ReleaseBatch:
        """Release an approved claim into the immutable ledger (idempotent)."""
        claim = self.get(repos, claim_id)

        idem = _idempotency_key(claim.client_id, claim.id)
        existing = repos.releases.entry_for(idem)
        if existing is not None:
            # Already released — idempotent no-op, no second entry.
            return repos.session.get(ReleaseBatch, existing.release_batch_id)

        if claim.status != "approved":
            raise IllegalTransition(f"cannot release a claim in status {claim.status!r}")

        digest = canonical_hash([self._projection(claim)])
        carbon_ref = f"CARB-{digest[:12].upper()}"
        token = StubTSA().stamp(digest)

        batch = repos.releases.add_batch(
            ReleaseBatch(
                firm_id=claim.firm_id,
                client_id=claim.client_id,
                source_type=SOURCE_TYPE,
                created_by=actor,
                batch_hash=digest,
                tsa_token=token,
                record_count=1,
                total_tco2e=claim.tco2e,
                status="released",
            )
        )
        repos.releases.add_entry(
            EmissionEntry(
                firm_id=claim.firm_id,
                client_id=claim.client_id,
                source_type=SOURCE_TYPE,
                source_id=claim.id,
                scope=claim.scope,
                factor_key=claim.factor_key,
                factor_version=claim.factor_version,
                quantity=claim.quantity,
                unit=claim.unit,
                basis=claim.basis,
                tco2e=claim.tco2e,
                release_batch_id=batch.id,
                idempotency_key=idem,
                carbon_ref=carbon_ref,
            )
        )
        claim.status = "released"
        StubSink().post(digest, 1)

        released = record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="released",
            actor=actor,
            detail={"batch_hash": digest, "carbon_ref": carbon_ref},
        )
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="tsa_anchored",
            actor="system",
            detail={"tsa_token": token},
            prev_hash=released.hash,
        )
        return batch

    def reverse(self, *, repos: "Repos", claim_id: uuid.UUID, actor: str) -> EmissionEntry:
        """Correct a released claim with a reversing (negative) entry.

        Never edits or deletes the original entry — irreversibility means a
        correction is a new, opposite-signed entry.
        """
        claim = self.get(repos, claim_id)
        if claim.status != "released":
            raise IllegalTransition("only a released claim can be reversed")

        idem = _idempotency_key(claim.client_id, claim.id, suffix="reversal")
        if repos.releases.entry_for(idem) is not None:
            raise IllegalTransition("claim already reversed")

        neg = -claim.tco2e
        projection = self._projection(claim) | {"reversal_of": str(claim.id), "tco2e": format(neg, "f")}
        digest = canonical_hash([projection])
        carbon_ref = f"CARB-REV-{digest[:12].upper()}"

        batch = repos.releases.add_batch(
            ReleaseBatch(
                firm_id=claim.firm_id,
                client_id=claim.client_id,
                source_type=SOURCE_TYPE,
                created_by=actor,
                batch_hash=digest,
                tsa_token=StubTSA().stamp(digest),
                record_count=1,
                total_tco2e=neg,
                status="released",
            )
        )
        entry = repos.releases.add_entry(
            EmissionEntry(
                firm_id=claim.firm_id,
                client_id=claim.client_id,
                source_type=SOURCE_TYPE,
                source_id=claim.id,
                scope=claim.scope,
                factor_key=claim.factor_key,
                factor_version=claim.factor_version,
                quantity=None if claim.quantity is None else -claim.quantity,
                unit=claim.unit,
                basis=claim.basis,
                tco2e=neg,
                release_batch_id=batch.id,
                idempotency_key=idem,
                carbon_ref=carbon_ref,
            )
        )
        record_event(
            repos.audit,
            firm_id=claim.firm_id,
            client_id=claim.client_id,
            entity_type="claim",
            entity_id=claim.id,
            event_type="reversed",
            actor=actor,
            detail={"carbon_ref": carbon_ref, "tco2e": format(neg, "f")},
        )
        return entry


@dataclass
class Repos:
    """Bundle of repositories sharing one session, for one request/operation."""

    session: object
    claims: ClaimRepository
    factors: FactorRepository
    releases: ReleaseRepository
    audit: AuditRepository

    @classmethod
    def for_session(cls, session) -> "Repos":
        return cls(
            session=session,
            claims=ClaimRepository(session),
            factors=FactorRepository(session),
            releases=ReleaseRepository(session),
            audit=AuditRepository(session),
        )


def _idempotency_key(client_id: uuid.UUID, claim_id: uuid.UUID, suffix: str = "") -> str:
    raw = f"{client_id}{claim_id}{suffix}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
