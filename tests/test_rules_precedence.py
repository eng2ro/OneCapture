"""FR-S3: item -> vendor -> GL precedence, and rule-version stamping."""

from __future__ import annotations

from decimal import Decimal

from erpsync.domain.models import SourceRecord
from erpsync.rules.engine import match_rule


def _rec(**kw) -> SourceRecord:
    base = dict(client_id="abc_manufacturing", doc_entry="1", vendor_name="X", amount=Decimal("100"))
    base.update(kw)
    return SourceRecord(**base)


def test_item_rule_beats_vendor_rule(ruleset):
    # DIESEL-B7 line at Petronas: item rule (Fleet diesel) must win over the
    # Petronas vendor rule (Bulk fuel).
    rec = _rec(item_code="DIESEL-B7", vendor_name="Petronas Dagangan Berhad")
    match = match_rule(rec, ruleset)
    assert match is not None
    assert match.rule.rule_id == "ITEM-DIESEL-B7"
    assert match.rule.category == "Fleet diesel"


def test_vendor_rule_used_when_no_item_rule(ruleset):
    rec = _rec(item_code="UNKNOWN-PUMP", vendor_name="Petronas Dagangan Berhad")
    match = match_rule(rec, ruleset)
    assert match.rule.rule_id == "VENDOR-PETRONAS"
    assert match.rule.category == "Bulk fuel"


def test_gl_rule_is_lowest_precedence(ruleset):
    rec = _rec(item_code="", vendor_name="KL Logistics", gl_account="5400")
    match = match_rule(rec, ruleset)
    assert match.rule.rule_id == "GL-FREIGHT-5400"


def test_vendor_match_is_case_insensitive_substring(ruleset):
    rec = _rec(item_code="", vendor_name="TENAGA NASIONAL BERHAD (TNB)")
    match = match_rule(rec, ruleset)
    assert match.rule.rule_id == "VENDOR-TENAGA"


def test_unmapped_line_returns_none(ruleset):
    rec = _rec(item_code="PAPER-A4", vendor_name="Office Depot", gl_account="6100")
    assert match_rule(rec, ruleset) is None


def test_rule_version_is_recorded(ruleset):
    rec = _rec(item_code="DIESEL-B7")
    match = match_rule(rec, ruleset)
    assert match.rule_version == "v7"
