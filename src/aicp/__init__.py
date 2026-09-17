"""AICP platform adapter — the ONE place this repo touches the platform.

Your agent code (src/agent/) should only ever import from here:

    from aicp import get_model, aget_mcp_tools, aget_skills

Everything platform-specific — the governed model gateway, how MCP servers are configured,
how skill bundles are pulled — lives in this package. When the platform's contracts evolve
(MCP runtime, token vending), you update THIS package from the template; your graph code
does not change.

Design invariant: with ZERO platform env set, every function here degrades gracefully
(no MCP tools -> [], no skills -> []) so the graph always builds and /ping stays healthy.
"""
from aicp.mcp import aget_mcp_tools
from aicp.model import get_model
from aicp.skills import aget_skills

__all__ = ["get_model", "aget_mcp_tools", "aget_skills"]
