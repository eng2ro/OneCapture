"""FR-S4: activity-first, spend-based fallback, with DQ flagging."""

from __future__ import annotations

from decimal import Decimal

from erpsync.domain.enums import DataQuality, QuantityBasis
from erpsync.quantity.resolver import resolve
from erpsync.rules.engine import match_rule
from erpsync.domain.models import SourceRecord


def _rec(**kw) -> SourceRecord:
    base = dict(client_id="c", doc_entry="1", vendor_name="X", amount=Decimal("100"))
    base.update(kw)
    return SourceRecord(**base)


def test_activity_basis_when_quantity_and_uom_present(ruleset, factors):
    rec = _rec(item_code="DIESEL-B7", quantity=Decimal("450"), uom="L")
    rule = match_rule(rec, ruleset).rule
    res = resolve(rec, rule, factors)
    assert res.basis is QuantityBasis.ACTIVITY
    assert res.data_quality is DataQuality.MEASURED
    assert res.quantity == Decimal("450")


def test_spend_fallback_when_quantity_missing(ruleset, factors):
    rec = _rec(item_code="DIESEL-B7", quantity=None, uom=None, amount=Decimal("800"))
    rule = match_rule(rec, ruleset).rule
    res = resolve(rec, rule, factors)
    assert res.basis is QuantityBasis.SPEND
    assert res.data_quality is DataQuality.ESTIMATED
    assert res.factor_ref == "EEIO_FUEL"
    assert res.amount == Decimal("800")
    assert res.notes  # carries an explanation


def test_uom_mismatch_falls_back_to_spend(ruleset, factors):
    # litres factor but the line is in gallons -> don't trust it, use spend.
    rec = _rec(item_code="DIESEL-B7", quantity=Decimal("100"), uom="GAL", amount=Decimal("500"))
    rule = match_rule(rec, ruleset).rule
    res = resolve(rec, rule, factors)
    assert res.basis is QuantityBasis.SPEND
    assert "UoM" in res.notes[0]


def test_spend_basis_rule_resolves_directly(ruleset, factors):
    rec = _rec(item_code="", vendor_name="KL Logistics", gl_account="5400", amount=Decimal("3200"))
    rule = match_rule(rec, ruleset).rule
    res = resolve(rec, rule, factors)
    assert res.basis is QuantityBasis.SPEND
    assert res.factor_ref == "EEIO_FREIGHT"


def test_unvaluable_when_no_quantity_and_no_fallback(factors):
    from erpsync.domain.models import MappingRule
    from erpsync.domain.enums import Scope

    rule = MappingRule(
        rule_id="X", item_code="WIDGET", category="Widget", scope=Scope.S1,
        basis=QuantityBasis.ACTIVITY, factor_ref="DIESEL_B7", spend_fallback_ref=None,
    )
    rec = _rec(item_code="WIDGET", quantity=None, uom=None)
    res = resolve(rec, rule, factors)
    assert res.factor_ref is None
    assert res.data_quality is DataQuality.FLAGGED
