"""Carbon *relevance* (e-Claim, post-0011).

e-Claim no longer does ANY carbon classification or tCO2e maths — that is
CarbonNext's job. The only carbon decision left is per category: is its spend
**carbon-relevant** (forwarded to CarbonNext) or not? This tiny module is the seam
that resolves that flag from a line's category; the service snapshots it onto the
``claim_line`` at capture, and the release step forwards the raw data of the
relevant lines.

The old factor/scope/basis classifier (and the emission-factor lookup) were
retired here — e-Claim keeps none of that. The legacy ``scope``/``factor_key``/
``basis``/``data_quality`` columns remain on the tables as vestigial nullables.
"""

from __future__ import annotations


def carbon_relevant_for(category) -> bool:
    """A line's ``carbon_relevant`` is its category's flag.

    An UNMAPPED line (no category assigned yet) defaults to ``True`` so it is never
    silently dropped from the CarbonNext handoff before a reviewer categorizes it —
    once a category is assigned, its flag is snapshotted in its place."""
    return True if category is None else bool(category.carbon_relevant)
