"""MCP tools from env-configured remote servers (streamable HTTP).

AICP_MCP_SERVERS is a one-line JSON list; each server is loaded INDEPENDENTLY so one dead
server never takes down the others (and never crashes startup — warn+skip unless AICP_STRICT=1).
Auth: "auth_env" names an env var holding a bearer token (value arrives via the AICP secret
box); "headers" adds arbitrary headers with ${VAR} expansion. Today's reality: public MCP
URLs + API keys. When AICP's governed MCP runtime + token-exchange land, only this file changes."""
from __future__ import annotations

import asyncio
import os

from aicp import config

log = config.log

_DEFAULT_TIMEOUT = 30.0


def _timeout_seconds() -> float:
    """AICP_MCP_TIMEOUT as a positive finite float; fall back to the default on a bad value so one
    typo can't abort tool loading for every server (non-strict graceful degradation)."""
    raw = os.environ.get("AICP_MCP_TIMEOUT")
    if not raw:
        return _DEFAULT_TIMEOUT
    try:
        val = float(raw)
        if val > 0 and val != float("inf"):
            return val
        raise ValueError("AICP_MCP_TIMEOUT must be a positive, finite number")
    except (ValueError, OverflowError):
        if config.strict():
            raise
        log.warning("AICP_MCP_TIMEOUT is not a valid number — using %.0fs", _DEFAULT_TIMEOUT)
        return _DEFAULT_TIMEOUT


def _connection(server: dict) -> dict:
    conn: dict = {"transport": "streamable_http", "url": server["url"]}
    headers: dict[str, str] = {}
    auth_env = server.get("auth_env")
    if auth_env:
        token = os.environ.get(auth_env, "")
        if not token:
            # Fail CLOSED: a server configured to need auth must not be reached anonymously (if the
            # endpoint happens to allow anonymous access, we'd expose it without its credential).
            raise RuntimeError(f"MCP server '{server['name']}': auth env var '{auth_env}' is empty/unset")
        headers["Authorization"] = f"Bearer {token}"
    for k, v in (server.get("headers") or {}).items():
        headers[str(k)] = os.path.expandvars(str(v))
    if headers:
        conn["headers"] = headers
    return conn


async def aget_mcp_tools() -> list:
    """LangChain tools from every reachable configured MCP server. [] when none configured."""
    servers = config.mcp_servers()
    if not servers:
        return []
    # imported lazily so an agent with no MCP config never even needs the adapter at runtime
    from langchain_mcp_adapters.client import MultiServerMCPClient

    timeout = _timeout_seconds()
    tools: list = []
    for server in servers:
        name = server["name"]
        try:
            client = MultiServerMCPClient({name: _connection(server)})
            server_tools = await asyncio.wait_for(client.get_tools(server_name=name), timeout=timeout)
            tools.extend(server_tools)
            log.info("MCP '%s': loaded %d tool(s)", name, len(server_tools))
        except Exception as e:
            if config.strict():
                raise
            # log the type + message only — never dump headers/URLs with credentials
            log.warning("MCP '%s' unavailable (%s) — skipped; agent runs without it", name, type(e).__name__)
    return tools
