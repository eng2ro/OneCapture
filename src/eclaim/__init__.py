"""OneCapture e-Claim — receipt capture → OCR → carbon → review → release.

A Postgres-backed module of the OneCapture app. Shares the carbon arithmetic,
release hashing, and audit chain with ERP Sync via :mod:`core`; persists to the
same database via :mod:`eclaim.db`.
"""
