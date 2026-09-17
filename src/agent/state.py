"""Graph state.

MessagesState, because the AICP runtime shell requires it: aicp/entrypoint.py
calls `graph.ainvoke({"messages": [...]})` and reads `result["messages"][-1]`.
So even though this agent's work is entirely deterministic and involves no
conversation, its inputs and outputs are message-shaped.

`table` carries the rendered report alongside the message, so a caller that
wants the text without parsing a chat message can read it directly.
"""
from __future__ import annotations

from langgraph.graph import MessagesState


class State(MessagesState):
    table: str
    sent: bool
    blocked: bool
