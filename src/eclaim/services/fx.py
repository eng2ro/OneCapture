"""Exchange rates (Appendix G-C): monthly currency → MYR per client.

CarbonNext consumes MYR ("Amount Spent (MYR)" on every Scope-3 spend field), so a
foreign line must carry a conversion before it forwards. Resolution order:

1. a human-entered ``fx_rate`` on the line — always wins (editable + audited);
2. this table's rate for the DOCUMENT's month (``rate_for``), auto-prefilled at
   capture/edit so the reviewer sees the derived MYR base immediately;
3. none — the line renders a "needs FX" hint and the release audit notes it.

Rates enter manually (admin page) today; ``pull_fx_rates`` on the ERP connector
and a CarbonNext pull are later seams (``source`` records provenance). Whoever
owns the rate must be the single source of truth — if OneCapture converts at a
different rate than CarbonNext, reconcile-by-reference breaks (F-C question).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ExchangeRate
from ..repositories import AuditRepository
from .audit import record_event

MYR = ("MYR", "RM", "")


def month_of(value: dt.date | None) -> dt.date | None:
    return value.replace(day=1) if value is not None else None


def rate_for(
    session: Session, client_id: uuid.UUID, currency: str | None, on_date: dt.date | None
) -> Decimal | None:
    """The table rate for ``currency`` in the month of ``on_date`` — or None.

    MYR (or an unknown currency/date) needs no conversion → None. No fallback to
    an adjacent month: a missing month's rate is entered, not guessed — a wrong
    silent rate is worse than a flagged missing one."""
    if not currency or currency.upper() in MYR or on_date is None:
        return None
    row = session.execute(
        select(ExchangeRate).where(
            ExchangeRate.client_id == client_id,
            ExchangeRate.currency == currency.upper(),
            ExchangeRate.period == month_of(on_date),
        )
    ).scalar_one_or_none()
    return row.rate_to_myr if row else None


def upsert_rate(
    session: Session,
    *,
    firm_id: uuid.UUID,
    client_id: uuid.UUID,
    currency: str,
    period: dt.date,
    rate_to_myr: Decimal,
    source: str = "manual",
    actor: str = "",
) -> ExchangeRate:
    """Create or update one (currency, month) rate — audited with old → new so a
    rate change during a dispute is answerable."""
    currency = currency.strip().upper()
    period = month_of(period)
    row = session.execute(
        select(ExchangeRate).where(
            ExchangeRate.client_id == client_id,
            ExchangeRate.currency == currency,
            ExchangeRate.period == period,
        )
    ).scalar_one_or_none()
    detail = {"currency": currency, "period": period.isoformat(), "source": source}
    if row is None:
        row = ExchangeRate(
            firm_id=firm_id, client_id=client_id, currency=currency, period=period,
            rate_to_myr=rate_to_myr, source=source, created_by=actor,
        )
        session.add(row)
        session.flush()
        detail["rate"] = str(rate_to_myr)
        event = "fx_rate_added"
    else:
        detail["from"] = str(row.rate_to_myr)
        detail["to"] = str(rate_to_myr)
        row.rate_to_myr = rate_to_myr
        row.source = source
        event = "fx_rate_changed"
    record_event(
        AuditRepository(session), firm_id=firm_id, client_id=client_id,
        entity_type="exchange_rate", entity_id=row.id, event_type=event,
        actor=actor or "system", detail=detail,
    )
    session.flush()
    return row


def list_rates(session: Session, client_ids) -> list[ExchangeRate]:
    if not client_ids:
        return []
    return list(session.execute(
        select(ExchangeRate)
        .where(ExchangeRate.client_id.in_(client_ids))
        .order_by(ExchangeRate.period.desc(), ExchangeRate.currency)
    ).scalars())


def delete_rate(session: Session, *, rate_id: uuid.UUID, actor: str) -> None:
    """Remove a rate (audited). Lines already converted keep their applied
    fx_rate — deletion only stops FUTURE auto-prefill for that month."""
    row = session.get(ExchangeRate, rate_id)
    if row is None:
        return
    record_event(
        AuditRepository(session), firm_id=row.firm_id, client_id=row.client_id,
        entity_type="exchange_rate", entity_id=row.id, event_type="fx_rate_deleted",
        actor=actor or "system",
        detail={"currency": row.currency, "period": row.period.isoformat(),
                "rate": str(row.rate_to_myr)},
    )
    session.delete(row)
    session.flush()
