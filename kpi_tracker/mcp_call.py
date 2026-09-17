"""The one place asyncio lives. Everything else in kpi_tracker stays synchronous.

The MCP client SDK is async-only -- there is no sync facade -- but this
codebase is synchronous end to end and its 288 tests run offline in 0.2s. So
the async boundary is confined to this file, behind an ordinary blocking
function, and `mcp` is imported INSIDE that function rather than at module
scope. That import pulls in pydantic, starlette, uvicorn and httpx2; keeping it
lazy is what stops the test suite paying for them.

TIMEOUTS -- THE ORDERING MATTERS
--------------------------------
A single collect call blocks for as long as the vendor takes, which has been
measured at up to 9m39s. Two ceilings are in play:

    server-side vendor poll   1800s   (kpi_mcp.brightdata.POLL_TIMEOUT_SECONDS)
    client-side read timeout  2100s   (DEFAULT_TIMEOUT_SECONDS below)

The client's must be the LARGER of the two. If the client gives up first, we
get a bare timeout with no information and the server keeps working for
nothing. If the server gives up first -- which is what this ordering ensures --
we get a clear "snapshot not ready after 1800s", which becomes an honest dash
with a reason attached. The SDK's own default read timeout is 300s, which is
already less than a measured real run, so it must be overridden.
"""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from kpi_tracker.redaction import redact

log = logging.getLogger(__name__)

# DERIVED from the server's vendor ceiling rather than set independently, so
# the ordering above cannot be got wrong by setting one and forgetting the
# other. Both read KPI_VENDOR_POLL_TIMEOUT; this one adds a margin so the
# server always gives up first and returns a reason.
#
# Read here rather than imported from kpi_mcp: the client must never import the
# server package (tests/test_import_boundary.py enforces it).
POLL_TIMEOUT_ENV_VAR = "KPI_VENDOR_POLL_TIMEOUT"
DEFAULT_POLL_TIMEOUT_SECONDS = 1800.0
CLIENT_MARGIN_SECONDS = 300.0


def configured_timeout() -> float:
    raw = os.environ.get(POLL_TIMEOUT_ENV_VAR)
    try:
        poll = float(raw) if raw else DEFAULT_POLL_TIMEOUT_SECONDS
    except ValueError:
        poll = DEFAULT_POLL_TIMEOUT_SECONDS
    if poll <= 0:
        poll = DEFAULT_POLL_TIMEOUT_SECONDS
    return poll + CLIENT_MARGIN_SECONDS


DEFAULT_TIMEOUT_SECONDS = configured_timeout()


class McpCallFailed(RuntimeError):
    """A tool call did not return a usable result. Always render as unknown."""


def call_tool(
    server_url: str,
    tool_name: str,
    arguments: dict,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Call one MCP tool and return its structured result as a plain dict.

    Raises McpCallFailed for every unhappy path, with a message safe to show an
    operator. The caller turns that into a failed fetch -- never into a zero.
    """
    try:
        return _run_blocking(
            lambda: _call(server_url, tool_name, arguments, timeout_seconds=timeout_seconds)
        )
    except McpCallFailed:
        raise
    except BaseException as exc:  # noqa: BLE001 - see the comment below
        # BaseException, not Exception, and deliberately so. The async
        # transport raises BaseExceptionGroup and asyncio.CancelledError, and
        # NEITHER is an Exception subclass -- so a cancelled transport would
        # sail past `except Exception` and take down the whole run, including
        # the blog column that has nothing to do with this server.
        raise McpCallFailed(redact(_describe(exc))) from None


def _run_blocking(make_coroutine):
    """Run a coroutine from synchronous code, loop or no loop.

    `LinkedInSource.fetch()` is synchronous -- that is the seam the whole
    pipeline, report layer and 367 tests are built on -- but this function is
    called from two very different places:

      * the CLI, where no event loop is running: asyncio.run() is correct;
      * inside a LangGraph node on AgentCore, where one ALREADY is, and
        asyncio.run() raises "cannot be called from a running event loop".

    The second case is not hypothetical: it is what the first AgentCore smoke
    test hit, and it turned every count into a dash. So when a loop is already
    running we hand the work to a worker thread with a loop of its own. The
    alternative -- making fetch() async -- would push `await` through the
    pipeline, the report layer and every test, to serve one deployment.

    The coroutine is created INSIDE the chosen branch, not before it: building
    it eagerly and then not awaiting it produces a "coroutine was never awaited"
    warning on the path that does not use it.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make_coroutine())

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-call") as pool:
        return pool.submit(lambda: asyncio.run(make_coroutine())).result()


async def _call(
    server_url: str, tool_name: str, arguments: dict, *, timeout_seconds: float
) -> dict:
    from mcp import Client  # lazy: keeps mcp off the offline test path

    async with Client(server_url, read_timeout_seconds=timeout_seconds) as client:
        result = await client.call_tool(tool_name, arguments)

    if result.is_error:
        # A ToolError raised by the server preserves its text here. A bare
        # exception on the server does not -- the SDK withholds it, which is
        # why every expected failure server-side is raised as ToolError.
        raise McpCallFailed(redact(_text_of(result)) or f"{tool_name} reported an error")

    if result.structured_content is None:
        # Only happens if a tool loses its typed return annotation. Better a
        # loud failure than silently reading an empty result as "no data".
        raise McpCallFailed(
            f"{tool_name} returned no structured content -- the server's tool is "
            "missing its typed return annotation"
        )
    return result.structured_content


def _text_of(result) -> str:
    return " ".join(getattr(block, "text", "") or "" for block in result.content).strip()


def _describe(exc: BaseException) -> str:
    """A one-line description, flattening exception groups.

    An unflattened BaseExceptionGroup stringifies to "(1 sub-exception)", which
    tells an operator nothing at all.
    """
    inner = getattr(exc, "exceptions", None)
    if inner:
        return "; ".join(_describe(e) for e in inner[:3])
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
