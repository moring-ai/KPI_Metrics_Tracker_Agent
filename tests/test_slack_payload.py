"""The Slack payload is built by a pure function, so it is asserted on
directly. No network, no model, no clock -- identical input, identical bytes."""

import json

import pytest

from kpi_tracker.models import UNKNOWN_MARKER, Report, Row
from kpi_tracker.slack_delivery import build_blocks, fallback_text

REPORT = Report(
    week_label="Thu 2026-08-27 to Wed 2026-09-02",
    week_start="2026-08-27",
    week_end="2026-09-02",
    tz_name="Asia/Kolkata",
    rows=[Row("Ashvath Narayan", 4, 1), Row("Balaji Nagaraj", 0, 0), Row("Ellakkiaa", None, 2)],
    linkedin_ok=True,
    blogs_ok=True,
    notes=["Target: 3 LinkedIn posts per person per week."],
)


def table_of(blocks):
    return next(block for block in blocks if block["type"] == "table")


def test_the_table_has_a_header_row_plus_one_row_per_person():
    rows = table_of(build_blocks(REPORT))["rows"]
    assert len(rows) == 1 + len(REPORT.rows)
    assert [cell["text"] for cell in rows[0]] == ["Name", "LinkedIn posts (target 3)", "Blogs"]


def test_the_three_columns_are_name_linkedin_blogs():
    rows = table_of(build_blocks(REPORT))["rows"]
    assert [cell["text"] for cell in rows[1]] == ["Ashvath Narayan", "4", "1"]
    assert [cell["text"] for cell in rows[2]] == ["Balaji Nagaraj", "0", "0"]


def test_an_unknown_count_renders_as_a_marker_not_a_zero():
    """An em dash rather than "?": it reads as "no value available" instead of
    a data-entry mistake, and cannot be confused with a number."""
    rows = table_of(build_blocks(REPORT))["rows"]
    assert [cell["text"] for cell in rows[3]] == ["Ellakkiaa", UNKNOWN_MARKER, "2"]
    assert UNKNOWN_MARKER == "\u2014"


def test_no_cell_is_ever_empty():
    """Slack rejects the whole message with invalid_blocks on an empty cell."""
    blank = Report("W", "2026-08-27", "2026-09-02", "Asia/Kolkata", [Row("", 0, 0)], True, True)
    for row in table_of(build_blocks(blank))["rows"]:
        for cell in row:
            assert cell["text"] != ""


def test_the_target_is_configurable_in_the_header():
    rows = table_of(build_blocks(REPORT, target_posts_per_week=5))["rows"]
    assert rows[0][1]["text"] == "LinkedIn posts (target 5)"


def test_fallback_text_is_always_set():
    """This is the push-notification text and what any surface that cannot
    render a table falls back to. An unset one shows as an empty notification."""
    text = fallback_text(REPORT)
    assert text and "Ashvath Narayan" in text and UNKNOWN_MARKER in text


def test_the_payload_is_within_slacks_documented_limits():
    blocks = build_blocks(REPORT)
    assert len(blocks) <= 50  # blocks per message
    header = next(b for b in blocks if b["type"] == "header")
    assert len(header["text"]["text"]) <= 150

    table = table_of(blocks)
    assert len(table["rows"]) <= 100  # rows per table
    assert all(len(row) <= 20 for row in table["rows"])  # cells per row
    total = sum(len(cell["text"]) for row in table["rows"] for cell in row)
    assert total <= 10_000  # characters per table
    assert len(table["block_id"]) <= 255


def test_a_fifteen_person_team_still_fits_comfortably():
    rows = [Row(f"Person Number {i}", i, i % 3) for i in range(15)]
    big = Report("W", "2026-08-27", "2026-09-02", "Asia/Kolkata", rows, True, True)
    table = table_of(build_blocks(big))
    assert len(table["rows"]) == 16
    assert sum(len(c["text"]) for r in table["rows"] for c in r) < 10_000


def test_the_payload_is_json_serialisable():
    json.dumps(build_blocks(REPORT))


def test_notes_appear_under_the_table():
    blocks = build_blocks(REPORT)
    contexts = [b for b in blocks if b["type"] == "context"]
    assert any("Target: 3" in element["text"] for b in contexts for element in b["elements"])


def test_the_window_is_shown_so_the_reader_knows_what_was_measured():
    blocks = build_blocks(REPORT)
    context = blocks[1]["elements"][0]["text"]
    assert "2026-08-27" in context and "2026-09-02" in context and "Asia/Kolkata" in context


def test_identical_input_produces_identical_bytes():
    assert json.dumps(build_blocks(REPORT)) == json.dumps(build_blocks(REPORT))


def test_the_context_line_tracks_the_configured_anchor_day():
    """It used to hardcode "Mon-Sun" while the shipped default anchor is
    Thursday, so the line under the header contradicted the header itself."""
    blocks = build_blocks(REPORT)
    context = blocks[1]["elements"][0]["text"]

    assert "Mon-Sun" not in context
    assert REPORT.week_label in context  # "Thu 2026-08-27 to Wed 2026-09-02"
    assert "Asia/Kolkata" in context
