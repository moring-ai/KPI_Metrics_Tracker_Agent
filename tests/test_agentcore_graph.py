"""The AgentCore deployment wrapper.

Thin on purpose -- the logic is `kpi_tracker`, tested elsewhere. What is tested
here is the seam: that the graph satisfies the AICP runtime contract, and that
the ways this deployment differs from the EC2 one behave.

The contract, from aicp/entrypoint.py: the runtime calls
`graph.ainvoke({"messages": [...]})` and reads `result["messages"][-1].content`.
So the graph must accept and return message-shaped state even though its work
is entirely deterministic.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("langgraph", reason="only the AgentCore extra needs LangGraph")

SRC = Path(__file__).parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def activity_url(handle: str, iso_utc: str) -> str:
    ms = int(datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return f"https://www.linkedin.com/posts/{handle}_x-activity-{(ms << 22) | 0x1F2E3D}-aB3x"


@pytest.fixture
def deployed(tmp_path, monkeypatch, blog_site):
    """A configured AgentCore deployment with the vendor stubbed.

    Mirrors what AICP injects: secrets as environment variables (there is no
    MCP transport on the platform, so the container holds the tokens).
    """
    import json

    from kpi_mcp import linkedin_server

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "members.json").write_text(json.dumps([
        {"name": "Alice", "linkedin_url": "https://www.linkedin.com/in/alice"},
        {"name": "Bob", "linkedin_url": "https://www.linkedin.com/in/bob"},
    ]))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "timezone": "Asia/Kolkata",
        "week_start_day": "thursday",
        "members_path": str(tmp_path / "data" / "members.json"),
        "history_path": str(tmp_path / "data" / "history.jsonl"),
        "linkedin": {"source": "fixture", "target_posts_per_week": 3,
                     "fixture_path": str(tmp_path / "unused.json")},
        "blog": {"base_url": "https://blog.test"},
        "mcp": {"linkedin_url": "unused", "slack_url": "unused"},
        "slack": {"destination_label": "#marketing-kpi"},
    }))

    monkeypatch.setenv("KPI_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("KPI_SECRET_BACKEND", "env")
    monkeypatch.setenv("KPI_LINKEDIN_SECRET_ID", "TEST_BD")
    monkeypatch.setenv("TEST_BD", "vendor-token")
    monkeypatch.setenv("KPI_SLACK_SECRET_ID", "TEST_SLACK")
    monkeypatch.setenv("TEST_SLACK", "xoxb-1111-2222-abcdefghij")
    monkeypatch.setenv("KPI_SLACK_DESTINATION", "C0TEST")
    monkeypatch.setenv("AGENT_SEND", "false")

    monkeypatch.setattr(
        linkedin_server, "collect_post_urls",
        lambda token, urls, session=None: {
            "ok": {u: ([activity_url("alice", "2026-08-21T09:00:00")] if "alice" in u else [])
                   for u in urls if "bob" not in u},
            "failed": {"https://www.linkedin.com/in/bob": "Crawler error: Minimal layout detected"},
        },
    )
    return config_path


async def invoke(prompt: str = "run"):
    from agent.graph import graph

    compiled = await graph()
    return await compiled.ainvoke({"messages": [{"role": "user", "content": prompt}]})


# -- the AICP runtime contract -------------------------------------------


async def test_the_graph_builds_and_returns_message_shaped_state(deployed):
    """aicp/entrypoint.py reads result["messages"][-1].content -- nothing else."""
    out = await invoke("run 2026-08-28")
    assert out["messages"], "the runtime shell reads messages[-1]"
    assert hasattr(out["messages"][-1], "content")
    assert "Weekly KPI" in out["messages"][-1].content


async def test_the_graph_is_an_async_zero_arg_factory():
    """The export form the template and the validator require."""
    import inspect

    from agent.graph import graph

    assert inspect.iscoroutinefunction(graph)
    assert not inspect.signature(graph).parameters


async def test_the_table_is_also_returned_as_its_own_field(deployed):
    """So a caller can read the report without parsing a chat message."""
    out = await invoke("run 2026-08-28")
    assert out["table"] == out["messages"][-1].content


# -- the behaviour that matters ------------------------------------------


async def test_unknown_is_still_not_zero_through_this_deployment(deployed):
    """The whole point: the AgentCore path must preserve the invariant.
    Bob's profile failed; Alice published once; nobody else posted."""
    table = (await invoke("run 2026-08-28"))["messages"][-1].content
    assert "Alice" in table and "Bob" in table
    assert "—" in table, "Bob's failed profile must render an em dash"
    assert "not 0" in table, "and the reason must be stated"


async def test_an_iso_date_in_the_prompt_selects_that_week(deployed):
    assert "2026-08-20" in (await invoke("run 2026-08-28"))["messages"][-1].content


async def test_a_prompt_with_no_date_uses_the_current_week(deployed):
    out = await invoke("run")
    assert "Weekly KPI" in out["messages"][-1].content


async def test_unparseable_text_is_ignored_rather_than_guessed(deployed):
    """A wrong week is a wrong report, so junk must not become a date."""
    plain = (await invoke("run"))["messages"][-1].content
    junk = (await invoke("please run the report now"))["messages"][-1].content
    assert plain.split("\n")[0] == junk.split("\n")[0]


async def test_agent_send_false_makes_it_a_dry_run(deployed, monkeypatch):
    monkeypatch.setenv("AGENT_SEND", "false")
    out = await invoke("run 2026-08-28")
    assert out["sent"] is False


async def test_a_configuration_problem_returns_a_message_not_an_exception(
    deployed, monkeypatch
):
    """The runtime shell turns an exception into 'agent invocation failed',
    which tells an operator nothing. A readable message is worth more."""
    monkeypatch.setenv("KPI_CONFIG_PATH", "/nonexistent/config.json")
    out = await invoke("run")
    assert "Configuration problem" in out["messages"][-1].content
    assert out["blocked"] is True


async def test_an_already_reported_week_says_so_and_fetches_nothing(
    deployed, monkeypatch
):
    from kpi_tracker import history
    from kpi_tracker.models import Report

    monkeypatch.setenv("AGENT_SEND", "true")
    config_path = deployed
    import json as _json

    history_path = _json.loads(config_path.read_text())["history_path"]
    history.append(
        history_path,
        Report("W", "2026-08-20", "2026-08-26", "Asia/Kolkata", [], True, True),
        sent=True,
    )
    out = await invoke("run 2026-08-28")
    assert "already been reported" in out["messages"][-1].content
    assert out["sent"] is False
