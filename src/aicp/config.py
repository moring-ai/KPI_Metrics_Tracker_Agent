"""Env-driven capability config. All AICP_* variables are OPTIONAL — absence means "none",
never an error. Values are set in the AICP deploy form (non-secret box) / Secrets Manager
box (secrets), so attaching MCP servers or skills later is an env change + redeploy — no code."""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("aicp")
logging.basicConfig(level=os.environ.get("AICP_LOG_LEVEL", "INFO"))


def strict() -> bool:
    """AICP_STRICT=1 -> a configured-but-unloadable MCP server/skill raises instead of warn+skip."""
    return os.environ.get("AICP_STRICT", "0") == "1"


def mcp_servers() -> list[dict]:
    """Parse AICP_MCP_SERVERS: a ONE-LINE JSON list of {"name","url",("auth_env"),("headers")}.
    Unset/empty -> []. Malformed JSON -> [] with a warning (or raise under AICP_STRICT)."""
    raw = os.environ.get("AICP_MCP_SERVERS", "").strip()
    if not raw:
        return []
    try:
        servers = json.loads(raw)
    except Exception as e:
        if strict():
            raise
        # Never echo `raw` — it can carry tokens / credential-bearing URLs.
        log.warning("AICP_MCP_SERVERS is not valid JSON (%s) — running with NO MCP tools. "
                    "It must be one-line JSON, e.g. [{\"name\":\"x\",\"url\":\"https://...\"}]",
                    type(e).__name__)
        return []
    if not isinstance(servers, list):
        if strict():
            raise ValueError("AICP_MCP_SERVERS must be a JSON list")
        log.warning("AICP_MCP_SERVERS must be a JSON list — running with NO MCP tools")
        return []
    # Validate each entry INDEPENDENTLY: one malformed server is skipped, not fatal to the rest.
    # Log by index/name only (never the raw dict, which may hold headers/tokens).
    out = []
    for i, s in enumerate(servers):
        if not isinstance(s, dict) or not s.get("name") or not s.get("url"):
            if strict():
                raise ValueError(f"MCP server #{i} needs 'name' and 'url'")
            log.warning("MCP server #%d is missing 'name'/'url' — skipped", i)
            continue
        out.append(s)
    return out


def skill_handles() -> list[str]:
    """Parse AICP_SKILLS: comma-separated skill handles ('invoice-helper' or 'skill:invoice-helper')."""
    raw = os.environ.get("AICP_SKILLS", "").strip()
    return [h.strip() for h in raw.split(",") if h.strip()] if raw else []


def skill_registry_url() -> str:
    return os.environ.get("AICP_SKILL_REGISTRY_URL", "").rstrip("/")


def skill_token() -> str:
    return os.environ.get("AICP_SKILL_TOKEN", "")
