"""The credential-free LinkedIn sources: fixture and manual CSV.

These two need no server, no vendor account and no network, which is exactly
why they survive the MCP migration untouched -- manual_csv is the escape hatch
for a week when the vendor or the server is broken.

The MCP-backed source is tested in test_mcp_linkedin.py, the vendor protocol
in test_mcp_brightdata.py.
"""

import json

import pytest
from kpi_tracker.models import FetchResult
from kpi_tracker.sources.fixture import FixtureSource
from kpi_tracker.sources.manual_csv import ManualCsvSource
from kpi_tracker.timewindow import last_week

WEEK = last_week("2026-09-03", "Asia/Kolkata", "thursday")  # Thu 08-27 .. Wed 09-02

# Decodes to 2026-08-28T09:00Z -- inside the window.
IN_WINDOW = "https://www.linkedin.com/posts/ashvath-narayanan_a-activity-7499027998312443453-aB3x"
# Decodes to 2026-07-01 -- outside.
OUT_OF_WINDOW = "https://www.linkedin.com/posts/ashvath-narayanan_b-activity-7478009502107643453-aB3x"


def test_fixture_source_counts_only_posts_inside_the_window(tmp_path, members):
    path = tmp_path / "posts.json"
    path.write_text(json.dumps([{"url": IN_WINDOW}, {"url": OUT_OF_WINDOW}]))
    result = FixtureSource(path).fetch(members, WEEK)
    assert result.ok and len(result.items) == 1
    assert result.items[0].handle == "ashvath-narayanan"


def test_a_missing_source_file_is_a_failure_not_an_empty_week(tmp_path, members):
    result = FixtureSource(tmp_path / "nope.json").fetch(members, WEEK)
    assert result.ok is False and result.items == []


def test_malformed_json_is_a_failure(tmp_path, members):
    path = tmp_path / "posts.json"
    path.write_text("{not json")
    assert FixtureSource(path).fetch(members, WEEK).ok is False


def test_csv_reads_urls_and_derives_the_author_from_the_permalink(tmp_path, members):
    path = tmp_path / "posts.csv"
    path.write_text(f"url\n{IN_WINDOW}\n")
    result = ManualCsvSource(path).fetch(members, WEEK)
    assert len(result.items) == 1 and result.items[0].handle == "ashvath-narayanan"


def test_csv_ignores_comments_and_blank_lines(tmp_path, members):
    path = tmp_path / "posts.csv"
    path.write_text(f"# paste post URLs below\nurl\n{IN_WINDOW}\n\n")
    assert len(ManualCsvSource(path).fetch(members, WEEK).items) == 1


def test_the_same_post_pasted_twice_is_counted_once(tmp_path, members):
    path = tmp_path / "posts.csv"
    path.write_text(f"url\n{IN_WINDOW}\n{IN_WINDOW}\n")
    result = ManualCsvSource(path).fetch(members, WEEK)
    assert len(result.items) == 1
    assert any("duplicate" in warning for warning in result.warnings)


def test_a_post_by_someone_not_in_members_is_warned_about_not_counted(tmp_path, members):
    stranger = IN_WINDOW.replace("ashvath-narayanan", "some-stranger")
    path = tmp_path / "posts.csv"
    path.write_text(f"url\n{stranger}\n")
    result = ManualCsvSource(path).fetch(members, WEEK)
    assert result.items == []
    assert any("not in members.json" in warning for warning in result.warnings)


def test_an_unparseable_url_is_reported_with_its_line_number(tmp_path, members):
    path = tmp_path / "posts.csv"
    path.write_text("url\nhttps://www.linkedin.com/in/ashvath-narayanan/\n")
    result = ManualCsvSource(path).fetch(members, WEEK)
    assert result.ok and result.items == []
    assert any("line 2" in warning for warning in result.warnings)


def test_every_source_returns_the_same_envelope(tmp_path, members):
    """The swap seam: config changes one line, nothing downstream notices."""
    path = tmp_path / "posts.json"
    path.write_text("[]")
    for source in (FixtureSource(path), ManualCsvSource(tmp_path / "x.csv")):
        assert isinstance(source.fetch(members, WEEK), FetchResult)
        assert hasattr(source, "name")
