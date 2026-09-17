"""The weekly KPI report as a LangGraph graph, for Bedrock AgentCore.

ONE NODE, NO MODEL CALL -- and that is deliberate.

This agent counts LinkedIn posts and blog posts and renders a table. Every step
is deterministic: the shape of the work is known in advance, and the one
fuzzy-looking part (matching a blog byline to a person) turned out to be an
exact join on the LinkedIn URL the blog's own JSON-LD publishes. A KPI number
about a named colleague should never be a model's output, so no model is asked
for one. `aicp.get_model()` remains available and unused; the deploy form's
model selection and virtual key are injected but never spent.

If this agent later grows a conversational surface ("why is X a dash?"), the
model belongs THERE -- choosing which tool to call and phrasing prose around
the numbers -- never computing the numbers.

HOW THE CREDENTIALS WORK HERE
-----------------------------
On the EC2 deployment the tokens live in two separate MCP server processes that
the client cannot read (see deploy/README.md). AICP does not support MCP today,
so on AgentCore the same servers are wired IN-PROCESS: `mcp.Client` accepts a
server object directly, so the identical code path runs with no transport.

Be clear-eyed about what that costs. In-process, the MCP layer is a library
seam, not a security boundary -- this container holds both tokens as
environment variables injected from the AICP secret box. That is a real
downgrade from the EC2 deployment, accepted because the platform cannot express
the alternative yet. What it buys is that BOTH deployments run the same tested
code, so the correctness properties (unknown is never zero) hold identically.
"""
from __future__ import annotations

import logging
import os

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from agent.state import State

log = logging.getLogger(__name__)

# The report is idempotent per week and the runtime gives us no scheduling, so
# an accidental double invocation must not double-post. Sending is opt-in:
# AGENT_SEND=false turns any invocation into a dry run.
SEND_ENV_VAR = "AGENT_SEND"
CONFIG_ENV_VAR = "KPI_CONFIG_PATH"


def _should_send() -> bool:
    return os.environ.get(SEND_ENV_VAR, "true").strip().lower() not in ("0", "false", "no")


def _week_override(state: State) -> str | None:
    """An ISO date in the prompt runs that week instead of the current one.

    The scheduler sends {"prompt": "run"}; a human debugging sends
    {"prompt": "2026-08-28"}. Anything unparseable is ignored rather than
    guessed at -- a wrong week is a wrong report.
    """
    from datetime import date

    for message in reversed(state.get("messages") or []):
        text = str(getattr(message, "content", message) or "").strip()
        for token in text.split():
            try:
                date.fromisoformat(token)
                return token
            except ValueError:
                continue
    return None


async def graph():
    """An async zero-arg factory, per the AICP template contract.

    Everything is resolved inside, never at import time, so the container's
    /ping is healthy immediately and a misconfiguration surfaces as a failed
    invocation with a readable message rather than a failed deploy.
    """
    # Imported here rather than at module scope: the template's invariant is
    # that the graph builds with ZERO platform env configured, and these pull
    # in the MCP servers.
    from kpi_mcp import linkedin_server, slack_server

    from kpi_tracker import config as config_module
    from kpi_tracker import logging_setup, pipeline, report
    from kpi_tracker.sources.mcp_linkedin import McpLinkedInSource

    logging_setup.configure()

    async def run_report(state: State) -> dict:
        config_path = os.environ.get(CONFIG_ENV_VAR, "config.json")
        try:
            config = config_module.load(config_path)
        except config_module.ConfigError as exc:
            log.error("configuration problem: %s", exc)
            return {
                "messages": [AIMessage(content=f"Configuration problem: {exc}")],
                "table": "",
                "sent": False,
                "blocked": True,
            }

        built, _decisions = pipeline.run(
            config,
            run_at=_week_override(state),
            send=_should_send(),
            # In-process MCP: same code, no transport. See the module docstring.
            linkedin_source=McpLinkedInSource(linkedin_server.mcp),
            slack_target=slack_server.mcp,
        )

        if built is None:
            note = "This week has already been reported. Nothing fetched, nothing sent."
            return {
                "messages": [AIMessage(content=note)],
                "table": "",
                "sent": False,
                "blocked": False,
            }

        table = report.render_text(built)
        if built.blocked:
            table += "\n\nNOT SENT - a guardrail blocked this report."
        return {
            "messages": [AIMessage(content=table)],
            "table": table,
            "sent": _should_send() and not built.blocked,
            "blocked": built.blocked,
        }

    builder = StateGraph(State)
    builder.add_node("run_report", run_report)
    builder.add_edge(START, "run_report")
    builder.add_edge("run_report", END)
    # No checkpointer: each weekly invocation is independent, and a report must
    # never resume a previous run's state.
    return builder.compile(name="kpi-weekly")
