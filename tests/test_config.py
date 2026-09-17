"""Config validation. A job that runs 52 times a year should not discover a
typo at the moment it tries to message the CTO."""

import json

import pytest

from kpi_tracker import config as config_module

BASE = {
    "timezone": "Asia/Kolkata",
    "week_start_day": "thursday",
    "linkedin": {"source": "fixture", "target_posts_per_week": 3},
    "mcp": {"linkedin_url": "http://127.0.0.1:8801/mcp", "slack_url": "http://127.0.0.1:8802/mcp"},
    "slack": {"destination_label": "#marketing-kpi"},
}


def write(tmp_path, **overrides):
    data = json.loads(json.dumps(BASE)) | overrides
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def test_a_valid_config_loads(tmp_path):
    config = config_module.load(write(tmp_path), env={})
    assert config.timezone == "Asia/Kolkata"
    assert config.week_start_day == "thursday"
    assert config.target_posts_per_week == 3


def test_a_missing_config_file_says_what_to_do(tmp_path):
    with pytest.raises(config_module.ConfigError, match="config.example.json"):
        config_module.load(tmp_path / "nope.json", env={})


def test_malformed_json_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{oops")
    with pytest.raises(config_module.ConfigError, match="not valid JSON"):
        config_module.load(path, env={})


def test_an_invalid_timezone_is_rejected(tmp_path):
    with pytest.raises(config_module.ConfigError, match="IANA"):
        config_module.load(write(tmp_path, timezone="Mars/Olympus"), env={})


def test_an_invalid_week_start_day_is_rejected(tmp_path):
    with pytest.raises(config_module.ConfigError, match="unknown weekday"):
        config_module.load(write(tmp_path, week_start_day="thorsday"), env={})


def test_an_unknown_linkedin_source_is_rejected(tmp_path):
    bad = {"source": "magic", "target_posts_per_week": 3}
    with pytest.raises(config_module.ConfigError, match="linkedin.source"):
        config_module.load(write(tmp_path, linkedin=bad), env={})


def test_a_nonsense_target_is_rejected(tmp_path):
    bad = {"source": "fixture", "target_posts_per_week": 0}
    with pytest.raises(config_module.ConfigError, match="positive int"):
        config_module.load(write(tmp_path, linkedin=bad), env={})


def test_the_client_does_not_configure_a_slack_destination_at_all(tmp_path):
    """It is set on the kpi-slack server, so nothing on this machine can
    redirect the weekly report at somebody else."""
    config = config_module.load(write(tmp_path, slack={}), env={})
    assert not hasattr(config, "slack_destination")
    assert config.slack_destination_label  # cosmetic, for dry-run output only


def test_the_mcp_source_without_a_server_url_fails_at_load_not_mid_run(tmp_path):
    mcp_source = {"source": "mcp", "target_posts_per_week": 3}
    with pytest.raises(config_module.ConfigError, match="mcp.linkedin_url"):
        config_module.load(write(tmp_path, linkedin=mcp_source, mcp={}), env={})


def test_the_client_config_carries_no_credentials_at_all(tmp_path):
    """The whole point of the MCP split. If a token can reach this object, it
    can reach this process, and the isolation is a convention rather than a
    structure."""
    config = config_module.load(write(tmp_path), env={"SLACK_BOT_TOKEN": "xoxb-x"})
    for attribute in ("slack_token", "brightdata_token"):
        assert not hasattr(config, attribute), f"{attribute} must not exist on Config"
    assert "xoxb" not in repr(config)


# -- members.json ----------------------------------------------------------


def members_file(tmp_path, data):
    path = tmp_path / "members.json"
    path.write_text(json.dumps(data))
    return path


def test_members_load_with_a_pasted_profile_url(tmp_path):
    path = members_file(
        tmp_path,
        [{"name": "Ashvath Narayan", "linkedin_url": "https://www.linkedin.com/in/ashvath-narayanan/"}],
    )
    member = config_module.load_members(path)[0]
    assert member.linkedin_handle == "ashvath-narayanan"


def test_members_accept_a_bare_handle_too(tmp_path):
    path = members_file(tmp_path, [{"name": "A", "linkedin_handle": "ashvath-narayanan"}])
    assert config_module.load_members(path)[0].linkedin_handle == "ashvath-narayanan"


def test_blog_author_is_optional(tmp_path):
    path = members_file(tmp_path, [{"name": "Arya Sharan", "linkedin_url": "https://www.linkedin.com/in/arya-sharan/"}])
    assert config_module.load_members(path)[0].blog_author is None


def test_a_member_without_a_name_is_rejected(tmp_path):
    path = members_file(tmp_path, [{"linkedin_url": "https://www.linkedin.com/in/x/"}])
    with pytest.raises(config_module.ConfigError, match="no name"):
        config_module.load_members(path)


def test_a_member_without_a_usable_url_is_rejected_with_an_example(tmp_path):
    path = members_file(tmp_path, [{"name": "A", "linkedin_url": "not a url"}])
    with pytest.raises(config_module.ConfigError, match="linkedin.com/in/"):
        config_module.load_members(path)


def test_the_same_person_listed_twice_is_rejected(tmp_path):
    path = members_file(
        tmp_path,
        [
            {"name": "A", "linkedin_url": "https://www.linkedin.com/in/x/"},
            {"name": "A again", "linkedin_url": "https://linkedin.com/in/X"},
        ],
    )
    with pytest.raises(config_module.ConfigError, match="twice"):
        config_module.load_members(path)


def test_an_empty_members_file_is_rejected(tmp_path):
    with pytest.raises(config_module.ConfigError, match="non-empty"):
        config_module.load_members(members_file(tmp_path, []))


def test_the_shipped_members_file_and_example_config_are_valid():
    """The files in the repo must actually work."""
    members = config_module.load_members("data/members.json")
    assert len(members) >= 1
    config = config_module.load("config.example.json", env={"BRIGHTDATA_API_TOKEN": "x"})
    assert config.week_start_day == "thursday"


def test_an_s3_history_uri_survives_loading(tmp_path, monkeypatch):
    """Path("s3://bucket/key") normalises to "s3:/bucket/key" -- one slash --
    which is not a valid URI and fails only at write time, after the report has
    been computed."""
    config = config_module.load(
        write(tmp_path), env={"KPI_HISTORY_PATH": "s3://kpi-bucket/kpi-tracker/history.jsonl"}
    )
    assert str(config.history_path) == "s3://kpi-bucket/kpi-tracker/history.jsonl"


def test_a_local_history_path_is_still_a_path(tmp_path):
    from pathlib import Path

    assert isinstance(config_module.load(write(tmp_path), env={}).history_path, Path)


def test_the_env_override_wins_over_the_file(tmp_path):
    """One bundled config.json serves EC2 (a file) and Lambda (an S3 URI)."""
    config = config_module.load(write(tmp_path), env={"KPI_HISTORY_PATH": "s3://b/k.jsonl"})
    assert str(config.history_path) == "s3://b/k.jsonl"
