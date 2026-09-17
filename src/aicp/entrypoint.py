"""AgentCore runtime shell — serves the graph on :8080 (POST /invocations, GET /ping).

This is what the AICP-injected Dockerfile runs (`python -m aicp.entrypoint`). It finds YOUR
graph via langgraph.json (the same file `langgraph dev` uses), builds it LAZILY on the first
invocation — so /ping is healthy immediately and a slow/broken MCP server can never block the
deploy — and maps AgentCore sessions onto LangGraph threads for conversation memory.

You normally never edit this file."""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import uuid
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp, BedrockAgentCoreContext

from aicp import config
from aicp.skills import set_request_token

log = config.log
app = BedrockAgentCoreApp()


def _inbound_token(context) -> str | None:
    """The inbound invoke JWT (Bearer) that AgentCore forwards to the container — the agent's OWN
    vended identity for this request (aud=agent:<handle>). Used for token-vending skill access:
    the shell forwards it to the skill registry, which authorizes the agent as its owning team.
    Read from the request context (or the SDK's per-request header contextvar as a fallback)."""
    hdrs = None
    try:
        hdrs = getattr(context, "request_headers", None) or BedrockAgentCoreContext.get_request_headers()
    except Exception:
        hdrs = None
    if not hdrs:
        return None
    auth = hdrs.get("Authorization") or hdrs.get("authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None

_graph = None
_graph_lock = asyncio.Lock()


def _graph_spec() -> tuple[Path, str]:
    """Locate the graph from langgraph.json: first entry of "graphs", "./path/file.py:var"."""
    for base in (Path.cwd(), Path(__file__).resolve().parents[2]):
        cfg = base / "langgraph.json"
        if cfg.is_file():
            graphs = json.loads(cfg.read_text()).get("graphs") or {}
            if not graphs:
                raise RuntimeError(f"{cfg} has no 'graphs' entry")
            spec = next(iter(graphs.values()))
            path_str, _, var = spec.partition(":")
            if not var:
                raise RuntimeError(f"graph spec '{spec}' must be './path/to/file.py:variable'")
            return (base / path_str).resolve(), var
    raise RuntimeError("langgraph.json not found — the repo must follow the AICP LangGraph template")


async def _build_graph():
    path, var = _graph_spec()
    mod_spec = importlib.util.spec_from_file_location("aicp_user_graph", path)
    module = importlib.util.module_from_spec(mod_spec)
    sys.modules["aicp_user_graph"] = module
    mod_spec.loader.exec_module(module)
    obj = getattr(module, var, None)
    if obj is None:
        raise RuntimeError(f"{path} does not define '{var}' (check langgraph.json)")
    # Support the documented export forms: a compiled graph, or a zero-arg (a)sync factory.
    if callable(obj) and not hasattr(obj, "ainvoke"):
        obj = obj()
    if inspect.isawaitable(obj):
        obj = await obj
    if not hasattr(obj, "ainvoke"):
        raise RuntimeError(f"'{var}' in {path.name} is not a compiled LangGraph graph")
    return obj


async def _get_graph():
    global _graph
    if _graph is None:
        async with _graph_lock:
            if _graph is None:
                log.info("building agent graph (first invocation)")
                _graph = await _build_graph()
    return _graph


@app.entrypoint
async def invoke(payload, context=None):
    """AgentCore invocation: {"prompt": "..."} (or {"messages": [...]}) -> {"result": "..."}.
    The AgentCore session id becomes the LangGraph thread id, so multi-turn sessions keep memory."""
    # Token vending: set THIS request's inbound agent JWT BEFORE building the graph. The graph is built
    # ONCE (lazily, on the first invocation) and bakes skills into the system prompt via aget_skills()
    # -> skill-registry /resolve, which authorizes as the agent using this token. Setting it AFTER the
    # build (the earlier order) left that first build with no credential, so skill /resolve 401'd and
    # every skill was silently skipped. Falls back to the configured creds when the JWT is absent.
    set_request_token(_inbound_token(context))
    try:
        graph = await _get_graph()
    except Exception:
        log.exception("agent graph failed to build")
        return {"error": "agent failed to initialize — check the agent logs"}
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not messages:
        prompt = (payload or {}).get("prompt") if isinstance(payload, dict) else str(payload)
        messages = [{"role": "user", "content": str(prompt or "Hello")}]
    # The session id is the LangGraph checkpoint (thread) key. Never share one across sessionless
    # invocations — a fixed "default" would let a later caller resume an earlier caller's history.
    session_id = getattr(context, "session_id", None) or f"anon-{uuid.uuid4()}"
    try:
        result = await graph.ainvoke({"messages": messages}, {"configurable": {"thread_id": session_id}})
        last = result["messages"][-1]
        return {"result": getattr(last, "content", str(last))}
    except Exception:
        log.exception("agent invocation failed")
        return {"error": "agent invocation failed — check the agent logs"}


if __name__ == "__main__":
    app.run()
