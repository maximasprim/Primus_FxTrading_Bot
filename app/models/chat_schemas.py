"""Schemas for the dashboard chat agent (app/services/chat_agent_service.py)."""
from typing import Literal

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    # Full conversation so far, oldest first, ending in the newest user
    # message. The client is the source of truth for history — this
    # service is stateless between requests, same as the rest of the API.
    messages: list[ChatMessage]
    actor: str = "dashboard-chat"


class ChatToolAction(BaseModel):
    """A record of a tool the agent actually invoked during this turn, so
    the UI can show exactly what happened (and the trade journal / audit
    log already records the execution-service side independently)."""

    tool: str
    input: dict
    summary: str


class ChatResponse(BaseModel):
    reply: str
    actions: list[ChatToolAction] = []
