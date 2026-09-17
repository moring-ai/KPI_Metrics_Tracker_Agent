"""The dead-man's alert.

The property worth protecting: an alerter that itself raises is worse than no
alerter, because it turns one failure into two and buries the original. Every
path here must return an exit code, never propagate.
"""

import json

from kpi_tracker import notify_failure


def write_config(tmp_path, slack_url="http://127.0.0.1:8802/mcp"):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "members.json").write_text(
        json.dumps([{"name": "A", "linkedin_url": "https://www.linkedin.com/in/a"}])
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "timezone": "Asia/Kolkata",
        "week_start_day": "thursday",
        "members_path": str(tmp_path / "data" / "members.json"),
        "history_path": str(tmp_path / "data" / "history.jsonl"),
        "linkedin": {"source": "fixture", "target_posts_per_week": 3},
        "mcp": {"linkedin_url": "http://x", "slack_url": slack_url},
        "slack": {"destination_label": "#marketing-kpi"},
    }))
    return path


def test_it_posts_an_alert_naming_the_host(tmp_path, monkeypatch):
    sent = {}

    def caller(server_url, tool_name, arguments, **kwargs):
        sent.update({"tool": tool_name, "text": arguments["text"]})
        return {"schema_version": 1, "status": "sent", "message_ts": "1.0"}

    monkeypatch.setattr(notify_failure, "call_tool", caller)
    assert notify_failure.main([str(write_config(tmp_path))]) == 0
    assert sent["tool"] == "post_alert"
    assert "failed" in sent["text"] and "journalctl" in sent["text"]


def test_a_missing_slack_server_is_logged_not_raised(tmp_path):
    """Nothing above this process can catch an exception."""
    assert notify_failure.main([str(write_config(tmp_path, slack_url=""))]) == 1


def test_an_unreadable_config_is_logged_not_raised(tmp_path):
    assert notify_failure.main([str(tmp_path / "nope.json")]) == 1


def test_a_failing_alert_call_is_logged_not_raised(tmp_path, monkeypatch):
    """Both the run and the alert failed. That must still exit cleanly."""
    from kpi_tracker.mcp_call import McpCallFailed

    def caller(*a, **k):
        raise McpCallFailed("connection refused")

    monkeypatch.setattr(notify_failure, "call_tool", caller)
    assert notify_failure.main([str(write_config(tmp_path))]) == 1
