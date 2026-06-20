"""Rule matching engine (FR-S3).

Precedence is **item → vendor → GL**: a rule that matches on the line's item
code wins over one matching the vendor name, which wins over one matching the
GL account. This mirrors specificity — an item code is the most precise carbon
signal, a GL account the least. The first match in precedence order is
returned along with the ruleset version that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import MappingRule, RuleSet, SourceRecord


@dataclass(frozen=True)
class RuleMatch:
    rule: MappingRule
    rule_version: str


_PRECEDENCE = ("item", "vendor", "gl")


def match_rule(record: SourceRecord, ruleset: RuleSet) -> RuleMatch | None:
    """Return the highest-precedence matching rule, or None if unmapped."""
    by_dim: dict[str, list[MappingRule]] = {d: [] for d in _PRECEDENCE}
    for rule in ruleset.rules:
        by_dim[rule.dimension].append(rule)

    for dim in _PRECEDENCE:
        for rule in by_dim[dim]:
            if _matches(record, rule):
                return RuleMatch(rule=rule, rule_version=ruleset.version)
    return None


def _matches(record: SourceRecord, rule: MappingRule) -> bool:
    if rule.dimension == "item":
        return (
            record.item_code is not None
            and record.item_code.strip().upper() == rule.item_code.strip().upper()
        )
    if rule.dimension == "vendor":
        return (
            record.vendor_name is not None
            and rule.vendor_match.strip().lower() in record.vendor_name.strip().lower()
        )
    # gl
    return (
        record.gl_account is not None
        and record.gl_account.strip() == rule.gl_account.strip()
    )
