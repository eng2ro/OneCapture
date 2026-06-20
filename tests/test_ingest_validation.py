"""FR-S1: row validation and structural classification."""

from __future__ import annotations

from erpsync.domain.enums import RowStatus
from erpsync.ingest.validate import validate_rows
from gen_synthetic import HEADER, month_rows


def _blank_row(**kw):
    row = {c: "" for c in HEADER}
    row.update(kw)
    return row


def test_clean_month_all_accepted(preset):
    result = validate_rows(month_rows(), preset, "abc_manufacturing")
    # Every row in the clean month parses structurally (no rejections here;
    # cross-channel dup is detected later in the pipeline, not in validation).
    assert len(result.accepted) == 7
    assert result.outcomes == []


def test_missing_required_field_is_rejected(preset):
    rows = [_blank_row(DocEntry="1", CardName="", LineTotal="100")]  # no vendor
    result = validate_rows(rows, preset, "c1")
    assert not result.accepted
    assert result.outcomes[0].status is RowStatus.REJECTED
    assert "vendor_name" in result.outcomes[0].messages[0]


def test_unparseable_amount_is_rejected(preset):
    rows = [_blank_row(DocEntry="1", CardName="V", LineTotal="N/A")]
    result = validate_rows(rows, preset, "c1")
    assert result.outcomes[0].status is RowStatus.REJECTED
    assert "unparseable" in result.outcomes[0].messages[0]


def test_in_file_repeat_key_is_duplicate(preset):
    rows = [
        _blank_row(DocEntry="9", LineNum="0", CardName="V", LineTotal="10"),
        _blank_row(DocEntry="9", LineNum="0", CardName="V", LineTotal="10"),
    ]
    result = validate_rows(rows, preset, "c1")
    assert len(result.accepted) == 1
    assert result.outcomes[0].status is RowStatus.DUPLICATE


def test_prior_batch_key_is_duplicate(preset):
    rows = [_blank_row(DocEntry="9", LineNum="0", CardName="V", LineTotal="10")]
    seen = {("c1", "9", 0)}
    result = validate_rows(rows, preset, "c1", seen_keys=seen)
    assert not result.accepted
    assert result.outcomes[0].status is RowStatus.DUPLICATE


def test_thousands_separator_amount_parses(preset):
    rows = [_blank_row(DocEntry="9", CardName="V", LineTotal="1,234.50")]
    result = validate_rows(rows, preset, "c1")
    assert result.accepted
    assert str(result.accepted[0].record.amount) == "1234.50"
