"""
Canonical, provider-agnostic types for talking to an LLM, with or without
tool use.

Anthropic, Gemini, and OpenAI each use a different wire format for
messages, tool calls, and tool results. Every concrete provider in this
package (anthropic_provider.py, gemini_provider.py, openai_provider.py)
translates between its own wire format and these types. Callers
(chat_agent_service.py, ai_decision.py) only ever work with AIMessage /
AIResponse / AIToolCall -- swapping which provider is active never
touches calling code.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AIToolCall:
    id: str
    name: str
    input: dict
    provider_metadata: dict | None = None


@dataclass
class AIMessage:
    """One turn in a canonical conversation.

    role: "user" | "assistant" | "tool"
      - "assistant" with tool_calls populated means the model wants to
        call one or more tools; content may be empty in that case.
      - "tool" is the result of one tool call: tool_call_id/name identify
        which call this answers, content is the JSON-serialized result.
    """

    role: str
    content: str | None = None
    tool_calls: list[AIToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class AIResponse:
    text: str
    tool_calls: list[AIToolCall] = field(default_factory=list)


class AIProviderError(Exception):
    """Raised with a message that's safe to show the user directly --
    never includes a raw API key. Callers decide whether to surface it
    (chat) or silently fall back to deterministic behavior (analysis)."""


class AIProvider:
    name: str = "base"

    async def complete(
        self,
        system: str,
        messages: list[AIMessage],
        tools: list[dict] | None = None,
        max_tokens: int = 1500,
    ) -> AIResponse:
        raise NotImplementedError
