"""OneCapture shared core.

Logic used by more than one capture module (ERP Sync, e-Claim):

* :mod:`core.carbon`  — exact Decimal tCO2e arithmetic.
* :mod:`core.release` — canonical batch hashing + TSA/sink seams.
* :mod:`core.audit`   — hash-chained audit events.

These are deliberately storage-agnostic: pure functions over plain values, so
both the ERP Sync engine and the Postgres-backed e-Claim module call the same
code without sharing a persistence layer.
"""
