import httpx

from app.ai.base import AIMessage, AIProvider, AIProviderError, AIResponse, AIToolCall

DEFAULT_MODEL = "claude-sonnet-5"


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str | None = None):
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL

    def _to_wire_messages(self, messages: list[AIMessage]) -> list[dict]:
        wire: list[dict] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            if m.role == "user":
                wire.append({"role": "user", "content": m.content or ""})
                i += 1
            elif m.role == "assistant":
                blocks = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
                wire.append({"role": "assistant", "content": blocks})
                i += 1
            elif m.role == "tool":
                # Anthropic requires every tool_result answering one
                # assistant turn's tool_use blocks to be batched into a
                # SINGLE following user message -- so collapse any run of
                # consecutive "tool" messages into one.
                result_blocks = []
                while i < len(messages) and messages[i].role == "tool":
                    t = messages[i]
                    result_blocks.append(
                        {"type": "tool_result", "tool_use_id": t.tool_call_id, "content": t.content or ""}
                    )
                    i += 1
                wire.append({"role": "user", "content": result_blocks})
            else:
                i += 1
        return wire

    async def complete(self, system, messages, tools=None, max_tokens=1500) -> AIResponse:
        if not self.api_key:
            raise AIProviderError("No Anthropic API key configured (ANTHROPIC_API_KEY).")

        payload: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": self._to_wire_messages(messages),
        }
        # Canonical tool shape (name/description/input_schema) already IS
        # Anthropic's native shape -- no translation needed.
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AIProviderError(_extract_error(exc)) from exc
            except httpx.HTTPError as exc:
                raise AIProviderError(f"Could not reach Anthropic: {exc}") from exc

        data = resp.json()
        blocks = data.get("content", [])
        text = "\n".join(b["text"] for b in blocks if b.get("type") == "text").strip()
        tool_calls = [
            AIToolCall(id=b["id"], name=b["name"], input=b.get("input", {}))
            for b in blocks
            if b.get("type") == "tool_use"
        ]
        return AIResponse(text=text, tool_calls=tool_calls)


def _extract_error(exc: httpx.HTTPStatusError) -> str:
    try:
        detail = exc.response.json().get("error", {}).get("message", exc.response.text)
    except Exception:  # noqa: BLE001
        detail = exc.response.text
    return f"Anthropic API error ({exc.response.status_code}): {detail}"
