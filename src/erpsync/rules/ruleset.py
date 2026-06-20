"""Versioned ruleset loader (FR-S3).

A ruleset is client-scoped and carries a ``version`` string that is stamped on
every emission entry it classifies, so an auditor can replay which rules
produced a figure. Rules are loaded from YAML; each rule declares exactly one
match dimension (item / vendor / gl).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..domain.enums import QuantityBasis, Scope
from ..domain.models import MappingRule, RuleSet


def _build_rule(raw: dict) -> MappingRule:
    dims = [k for k in ("item_code", "vendor_match", "gl_account") if raw.get(k)]
    if len(dims) != 1:
        raise ValueError(
            f"Rule {raw.get('rule_id')!r} must declare exactly one of "
            f"item_code/vendor_match/gl_account, found {dims}"
        )
    return MappingRule(
        rule_id=str(raw["rule_id"]),
        item_code=raw.get("item_code"),
        vendor_match=raw.get("vendor_match"),
        gl_account=raw.get("gl_account"),
        category=str(raw["category"]),
        scope=Scope(raw["scope"]),
        basis=QuantityBasis(raw["basis"]),
        factor_ref=str(raw["factor_ref"]),
        spend_fallback_ref=(
            str(raw["spend_fallback_ref"]) if raw.get("spend_fallback_ref") else None
        ),
    )


def load_ruleset(path: str | Path) -> RuleSet:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rules = tuple(_build_rule(r) for r in data.get("rules", []))
    rule_ids = [r.rule_id for r in rules]
    if len(rule_ids) != len(set(rule_ids)):
        raise ValueError("Duplicate rule_id in ruleset")
    return RuleSet(
        client_id=str(data["client_id"]),
        version=str(data["version"]),
        rules=rules,
    )
