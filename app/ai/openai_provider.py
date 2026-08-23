import json

import httpx

from app.ai.base import AIMessage, AIProvider, AIProviderError, AIResponse, AIToolCall

DEFAULT_MODEL = "gpt-4.1"


class OpenAIProvider(AIProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str | None = None):
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL

    def _to_wire_messages(self, system: str, messages: list[AIMessage]) -> list[dict]:
        wire: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "user":
                wire.append({"role": "user", "content": m.content or ""})
            elif m.role == "assistant":
                entry: dict = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
                        }
                        for tc in m.tool_calls
                    ]
                wire.append(entry)
            elif m.role == "tool":
                wire.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or ""})
        return wire

    def _to_wire_tools(self, tools: list[dict] | None) -> list[dict] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

    async def complete(self, system, messages, tools=None, max_tokens=1500) -> AIResponse:
        if not self.api_key:
            raise AIProviderError("No OpenAI API key configured (OPENAI_API_KEY).")

        payload: dict = {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "messages": self._to_wire_messages(system, messages),
        }
        wire_tools = self._to_wire_tools(tools)
        if wire_tools:
            payload["tools"] = wire_tools

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "content-type": "application/json"},
                    json=payload,
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AIProviderError(_extract_error(exc)) from exc
            except httpx.HTTPError as exc:
                raise AIProviderError(f"Could not reach OpenAI: {exc}") from exc

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        text = message.get("content") or ""
        tool_calls = []
        for tc in message.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(AIToolCall(id=tc["id"], name=fn.get("name", ""), input=args))
        return AIResponse(text=text, tool_calls=tool_calls)


def _extract_error(exc: httpx.HTTPStatusError) -> str:
    try:
        detail = exc.response.json().get("error", {}).get("message", exc.response.text)
    except Exception:  # noqa: BLE001
        detail = exc.response.text
    return f"OpenAI API error ({exc.response.status_code}): {detail}"
