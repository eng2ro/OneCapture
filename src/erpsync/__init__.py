"""OneCapture ERP Sync — import-mode capture pipeline (v1, Phase 1).

Reads a standardised AP invoice listing (CSV/XLSX) exported from an ERP,
validates and classifies every row, maps carbon-relevant lines through a
versioned rules engine, resolves quantities (activity-first, spend-based
fallback), computes tCO2e, screens cross-channel duplicates against e-Claim,
and produces a deterministic batch hash that is the input to the release gate.

External services (RFC 3161 TSA, Carbon Next ingestion) are stubbed behind
clean seams; everything here runs offline against synthetic data.
"""

__version__ = "0.1.0"
