"""Model access — ALWAYS through the governed AICP gateway (LiteLLM, OpenAI-compatible).

The platform injects OPENAI_BASE_URL / OPENAI_API_KEY / AGENT_MODEL at deploy; there are no
provider keys (Anthropic/Bedrock/OpenAI) in the container, so calling providers directly will
not work — and is exactly what the governance model forbids."""
from __future__ import annotations

import os

from langchain_openai import ChatOpenAI


def get_model(model: str | None = None, **kwargs) -> ChatOpenAI:
    """A chat model bound to the governed gateway.

    model: an APPROVED model alias (defaults to the AGENT_MODEL the deploy form selected).
    kwargs: passed through to ChatOpenAI (temperature, max_tokens, ...).
    """
    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LITELLM_API_KEY")
    if not base_url or not api_key:
        raise RuntimeError(
            "OPENAI_BASE_URL / OPENAI_API_KEY are not set. On AICP these are injected at deploy; "
            "for local dev copy .env.example to .env and point them at the platform gateway."
        )
    return ChatOpenAI(
        model=model or os.environ.get("AGENT_MODEL", "claude-sonnet"),
        base_url=base_url,
        api_key=api_key,
        **kwargs,
    )
