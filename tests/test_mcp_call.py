"""The synchronous bridge over the async MCP client.

The cases that matter here are the failure ones. Every unhappy path must become
McpCallFailed, because the caller turns that into a dash -- and anything that
escapes instead takes down the whole run, including the blog column that has
nothing to do with the LinkedIn server.
"""

import asyncio

import pytest

from kpi_tracker import mcp_call
from kpi_tracker.mcp_call import DEFAULT_TIMEOUT_SECONDS, McpCallFailed, _describe


class FakeResult:
    def __init__(self, *, is_error=False, structured=None, text=""):
        self.is_error = is_error
        self.structured_content = structured
        self.content = [type("Block", (), {"text": text})()] if text else []


def test_a_successful_call_returns_the_structured_dict(monkeypatch):
    async def fake(*a, **k):
        return {"schema_version": 1, "profiles": []}

    monkeypatch.setattr(mcp_call, "_call", fake)
    assert mcp_call.call_tool("http://x", "t", {}) == {"schema_version": 1, "profiles": []}


def test_a_tool_error_becomes_mcp_call_failed_with_its_text(monkeypatch):
    async def fake(*a, **k):
        raise McpCallFailed("vendor failure: snapshot not ready after 1800s")

    monkeypatch.setattr(mcp_call, "_call", fake)
    with pytest.raises(McpCallFailed, match="snapshot not ready"):
        mcp_call.call_tool("http://x", "t", {})


@pytest.mark.parametrize("exc", [
    ConnectionRefusedError("connection refused"),
    TimeoutError("read timeout"),
    RuntimeError("something odd"),
    OSError("no route to host"),
])
def test_any_ordinary_exception_becomes_mcp_call_failed(monkeypatch, exc):
    async def fake(*a, **k):
        raise exc

    monkeypatch.setattr(mcp_call, "_call", fake)
    with pytest.raises(McpCallFailed):
        mcp_call.call_tool("http://x", "t", {})


def test_a_cancelled_transport_does_not_escape(monkeypatch):
    """CancelledError is NOT an Exception subclass. Catching only Exception
    would let it past and kill the blog column too."""
    async def fake(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mcp_call, "_call", fake)
    with pytest.raises(McpCallFailed, match="CancelledError"):
        mcp_call.call_tool("http://x", "t", {})


def test_an_exception_group_does_not_escape_and_keeps_its_detail(monkeypatch):
    """anyio raises BaseExceptionGroup, also not an Exception. And an
    unflattened group stringifies to "(1 sub-exception)", which tells an
    operator nothing."""
    async def fake(*a, **k):
        raise BaseExceptionGroup("transport", [ConnectionRefusedError("connection refused")])

    monkeypatch.setattr(mcp_call, "_call", fake)
    with pytest.raises(McpCallFailed, match="connection refused"):
        mcp_call.call_tool("http://x", "t", {})


def test_a_credential_in_a_transport_error_is_redacted(monkeypatch):
    async def fake(*a, **k):
        raise RuntimeError("BRIGHTDATA_API_TOKEN=deadbeef-0000-4000-8000-000000000000 refused")

    monkeypatch.setattr(mcp_call, "_call", fake)
    with pytest.raises(McpCallFailed) as caught:
        mcp_call.call_tool("http://x", "t", {})
    assert "deadbeef" not in str(caught.value)
    assert "REDACTED" in str(caught.value)


def test_describe_flattens_nested_groups():
    group = BaseExceptionGroup(
        "outer", [BaseExceptionGroup("inner", [ValueError("deep detail")])]
    )
    assert "deep detail" in _describe(group)


def test_describe_names_the_type_when_there_is_no_message():
    assert _describe(asyncio.CancelledError()) == "CancelledError"


def test_the_default_timeout_exceeds_the_servers_own_vendor_ceiling():
    """Ordering matters: if the client gives up first we get a bare timeout
    with no information, and the server keeps working for nothing."""
    from kpi_mcp.brightdata import POLL_TIMEOUT_SECONDS

    assert DEFAULT_TIMEOUT_SECONDS > POLL_TIMEOUT_SECONDS
    # ...and comfortably above the SDK's own 300s default, which is already
    # less than a real measured run (9m39s).
    assert DEFAULT_TIMEOUT_SECONDS > 300


# -- running from inside an event loop (the AgentCore case) ---------------


def test_it_works_from_synchronous_code_with_no_loop(monkeypatch):
    async def fake(*a, **k):
        return {"schema_version": 1, "from": "no-loop"}

    monkeypatch.setattr(mcp_call, "_call", fake)
    assert mcp_call.call_tool("http://x", "t", {})["from"] == "no-loop"


def test_it_works_from_inside_a_running_event_loop(monkeypatch):
    """LangGraph nodes are async, so on AgentCore this is called with a loop
    already running -- where asyncio.run() raises. Caught by the first real
    smoke test, which turned every count into a dash."""
    async def fake(*a, **k):
        return {"schema_version": 1, "from": "inside-loop"}

    monkeypatch.setattr(mcp_call, "_call", fake)

    async def inside():
        return mcp_call.call_tool("http://x", "t", {})

    assert asyncio.run(inside())["from"] == "inside-loop"


def test_a_failure_inside_a_running_loop_still_becomes_mcp_call_failed(monkeypatch):
    async def fake(*a, **k):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(mcp_call, "_call", fake)

    async def inside():
        with pytest.raises(McpCallFailed, match="connection refused"):
            mcp_call.call_tool("http://x", "t", {})
        return True

    assert asyncio.run(inside())


def test_no_coroutine_is_left_unawaited(monkeypatch, recwarn):
    """Building the coroutine before choosing a branch leaks a RuntimeWarning."""
    async def fake(*a, **k):
        return {"schema_version": 1}

    monkeypatch.setattr(mcp_call, "_call", fake)
    mcp_call.call_tool("http://x", "t", {})
    assert not [w for w in recwarn if "never awaited" in str(w.message)]
