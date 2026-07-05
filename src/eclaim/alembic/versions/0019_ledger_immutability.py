"""Append-only ledger/audit at the DB level (B2)

Revision ID: 0019_ledger_immutability
Revises: 0018_ingestion_job
Create Date: 2026-07-05

The core promise is a tamper-evident carbon/audit trail. Until now that was
enforced only in Python: the ``onecapture_app`` role still held ``UPDATE`` and
``DELETE`` on the ledger and audit tables (granted broadly in 0002), so a bug —
or a compromised app process — could rewrite or erase released records.

Make the four append-only tables immutable at the database level for the app
role: REVOKE UPDATE, DELETE while keeping INSERT + SELECT. Corrections are made
by appending a reversing row (the ledger's design), never by mutating history.

Owner-role migrations (which run as the table owner, not ``onecapture_app``) are
unaffected, so a future schema change can still ALTER these tables.

Note: 0002's ALTER DEFAULT PRIVILEGES only grants on tables created *after* it,
so these already-existing tables need an explicit REVOKE here; no default-
privilege change is required.
"""

from alembic import op

revision = "0019_ledger_immutability"
down_revision = "0018_ingestion_job"
branch_labels = None
depends_on = None

# The append-only tables: carbon handoff to CarbonNext, the emission ledger, the
# tamper-evident audit chain, and the release batches that seal each export.
_LEDGER_TABLES = "emission_entry, carbon_handoff, audit_event, release_batch"

UPGRADE = f"""
REVOKE UPDATE, DELETE ON {_LEDGER_TABLES} FROM onecapture_app;
"""

DOWNGRADE = f"""
GRANT UPDATE, DELETE ON {_LEDGER_TABLES} TO onecapture_app;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
