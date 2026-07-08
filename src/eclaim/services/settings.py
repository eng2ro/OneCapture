"""Per-client settings registry (owner request 2026-07-08).

The Appendix-B rule made concrete: every behavioural CONTROL is a setting a
firm admin flips per company — never a per-customer code branch. Each key is
declared here with its allowed values, default and description; the admin UI
renders FROM this registry, so adding a control is one entry, no schema change.

Deliberately NOT settable: integrity rules — SoD, the append-only ledger, the
post-approval switch lock, attestation at release. Configuration governs
behaviour, never integrity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ClientSetting
from ..repositories import AuditRepository
from .audit import record_event


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    choices: tuple[str, ...]          # first entry = safe fallback for junk values
    default: str
    description: str


# The registry. The CarbonNext reversal-disposition flags join here once their
# API is confirmed (Appendix F-F); the proof-of-payment threshold (Appendix A
# layer 4) joins as a numeric setting when built.
REGISTRY: tuple[Setting, ...] = (
    Setting(
        key="carbon.auto_reverse",
        label="Carbon reversals",
        choices=("allow", "approver_reason", "off"),
        default="allow",
        description=(
            "allow: any authorised editor may reverse a released claim's carbon "
            "records. approver_reason: only a manager/partner may reverse, and a "
            "written reason is required (recommended once real data flows). "
            "off: reversals are disabled — corrections are coordinated manually "
            "with the carbon team."
        ),
    ),
    Setting(
        key="fx.auto_prefill",
        label="FX auto-prefill",
        choices=("on", "off"),
        default="on",
        description=(
            "on: a foreign receipt automatically picks up its month's exchange "
            "rate from the rate table (a reviewer-entered rate always wins). "
            "off: every FX rate is keyed by a human."
        ),
    ),
)

_BY_KEY = {s.key: s for s in REGISTRY}


def get(session: Session, client_id: uuid.UUID, key: str) -> str:
    """The effective value: the stored row if valid, else the registry default.
    A stored value no longer in the registry's choices falls back to the default
    (an old value never grants a behaviour the current code doesn't define)."""
    spec = _BY_KEY[key]
    row = session.execute(
        select(ClientSetting).where(
            ClientSetting.client_id == client_id, ClientSetting.key == key
        )
    ).scalar_one_or_none()
    if row is None or row.value not in spec.choices:
        return spec.default
    return row.value


def set_setting(
    session: Session, *, firm_id: uuid.UUID, client_id: uuid.UUID,
    key: str, value: str, actor: str,
) -> ClientSetting:
    """Upsert one setting — validated against the registry, audited old → new."""
    spec = _BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"unknown setting {key!r}")
    if value not in spec.choices:
        raise ValueError(f"{key}: {value!r} is not one of {spec.choices}")
    row = session.execute(
        select(ClientSetting).where(
            ClientSetting.client_id == client_id, ClientSetting.key == key
        )
    ).scalar_one_or_none()
    old = row.value if row is not None else spec.default
    if row is None:
        row = ClientSetting(
            firm_id=firm_id, client_id=client_id, key=key, value=value,
            updated_by=actor,
        )
        session.add(row)
    else:
        row.value = value
        row.updated_by = actor
    session.flush()
    if old != value:
        record_event(
            AuditRepository(session), firm_id=firm_id, client_id=client_id,
            entity_type="client_setting", entity_id=row.id,
            event_type="setting_changed", actor=actor or "system",
            detail={"key": key, "from": old, "to": value},
        )
        session.flush()
    return row


def effective(session: Session, client_id: uuid.UUID) -> dict[str, str]:
    """Every registry key's effective value for one client (the admin page)."""
    return {s.key: get(session, client_id, s.key) for s in REGISTRY}
