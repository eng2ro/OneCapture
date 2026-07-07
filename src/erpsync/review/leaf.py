"""Carbon-leaf state for a staged ERP Sync row (Appendix D / punch-list R3).

The review UI shows a green "leaf" when a staged row's carbon posts to CarbonNext on
release, and a muted dash otherwise. Getting that right means reconciling TWO
independent facts about the row, which an earlier version conflated by reading the
category alone:

* is it *carbon-related*? — did a mapping rule match (``category != 'UNMAPPED'``);
* will it actually *post*? — is its status one the release path projects
  (``clean``/``approved``), still pending review (``held``/``flagged`` → posts once
  approved), terminal (``dismissed``/``rejected_duplicate`` → never posts), or already
  ``released``.

Deriving the leaf from ``category`` alone gave two wrong signals: an approved-as-is
UNMAPPED row *posts* yet showed a bare dash ("nothing happens"), and a dismissed but
mapped row still claimed "posts on release". This module is the single source of
truth so both templates (queue + entry) stay honest and consistent — it reuses the
release path's :data:`RELEASABLE_STATUSES` and the review service's
:data:`REVIEWABLE`, so the leaf can never drift from what actually releases.
"""

from __future__ import annotations

from dataclasses import dataclass

from erpsync.release.service import RELEASABLE_STATUSES
from erpsync.review.service import REVIEWABLE

UNMAPPED = "UNMAPPED"

# Terminal review outcomes whose carbon will never post to CarbonNext.
_DISMISSED = ("dismissed", "rejected_duplicate")


@dataclass(frozen=True)
class LeafState:
    """How to render the carbon leaf for one staged row."""

    carbon: bool   # True → green leaf (carbon-related); False → muted dash
    muted: bool    # a carbon-related row that will NOT post (dismissed) → dimmed leaf
    label: str     # short text for the entry-detail view
    tooltip: str   # the honest hover / aria description


def carbon_leaf_state(status: str, category: str | None) -> LeafState:
    """Reconcile carbon-relatedness with releasability into one honest leaf state."""
    mapped = (category or UNMAPPED) != UNMAPPED

    if mapped:
        if status in _DISMISSED:
            # Suppress the "posts on release" claim for a dismissed row (R3): it is
            # carbon-related but will never post — say exactly that.
            return LeafState(
                True, True, "Carbon-related",
                "Carbon-related, but dismissed — will not post to CarbonNext",
            )
        if status == "released":
            return LeafState(
                True, False, "Carbon-related", "Carbon-related — posted to CarbonNext"
            )
        if status in RELEASABLE_STATUSES:
            return LeafState(
                True, False, "Carbon-related",
                "Carbon-related — posts to CarbonNext on release",
            )
        if status in REVIEWABLE:
            return LeafState(
                True, False, "Carbon-related",
                "Carbon-related — posts to CarbonNext once approved & released",
            )
        # Any other (future) status: carbon-related, posting not yet determined.
        return LeafState(True, False, "Carbon-related", "Carbon-related")

    # Unmapped: no carbon factor. If the row will still be projected on release, the
    # dash must not read as "nothing happens" — an approved-as-is / clean / released
    # UNMAPPED row lands in the ledger without a carbon figure (R3).
    if status in RELEASABLE_STATUSES or status == "released":
        return LeafState(
            False, False, "Not carbon-related",
            "No carbon factor — posts to the ledger without a carbon figure",
        )
    return LeafState(False, False, "Not carbon-related", "Not carbon-related")
