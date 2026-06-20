"""Per-request Postgres tenant context.

Every request runs its queries under two session-local settings that the RLS
policies read:

* ``app.current_firm``    — the firm uuid,
* ``app.allowed_clients`` — comma-joined client uuids the principal may touch.

``set_config(..., is_local => true)`` is the parameterised form of ``SET LOCAL``:
it lasts for the current transaction only, so context never leaks between
requests (each request is one transaction). With nothing set, ``current_setting``
returns NULL and the policies deny by default (zero rows).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

_SET_FIRM = text("SELECT set_config('app.current_firm', :v, true)")
_SET_CLIENTS = text("SELECT set_config('app.allowed_clients', :v, true)")


def set_firm_context(session: Session, firm_id: uuid.UUID) -> None:
    """Set only the firm — used during principal bootstrap, before the allowed
    client set is known (the firm-scoped tables only need this)."""
    session.execute(_SET_FIRM, {"v": str(firm_id)})


def set_tenant_context(
    session: Session, firm_id: uuid.UUID, allowed_client_ids: Iterable[uuid.UUID]
) -> None:
    """Set both firm and the allowed-client list for the rest of the request."""
    joined = ",".join(str(c) for c in allowed_client_ids)
    session.execute(_SET_FIRM, {"v": str(firm_id)})
    session.execute(_SET_CLIENTS, {"v": joined})
