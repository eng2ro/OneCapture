"""Carbon *relevance* unit tests (no DB).

e-Claim no longer classifies carbon (no scope/factor/tCO2e) — CarbonNext does. The
only decision left is per category: ``carbon_relevant`` (forward to CarbonNext?).
``carbon_relevant_for`` resolves a line's flag from its category, defaulting an
unmapped line to True so it is never silently dropped before review.
"""

from __future__ import annotations

from dataclasses import dataclass

from eclaim.services.classify import carbon_relevant_for


@dataclass
class _Cat:
    carbon_relevant: bool


def test_relevant_category():
    assert carbon_relevant_for(_Cat(carbon_relevant=True)) is True


def test_non_relevant_category():
    assert carbon_relevant_for(_Cat(carbon_relevant=False)) is False


def test_unmapped_defaults_to_relevant():
    # No category yet → True, so it is not dropped before a reviewer categorizes it.
    assert carbon_relevant_for(None) is True
