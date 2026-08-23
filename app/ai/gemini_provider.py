import json

import httpx

from app.ai.base import AIMessage, AIProvider, AIProviderError, AIResponse, AIToolCall

DEFAULT_MODEL = "gemini-3.5-flash-lite"


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str | None = None):
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL

    def _to_wire_contents(self, messages: list[AIMessage]) -> list[dict]:
        contents: list[dict] = []
        for m in messages:
            if m.role == "user":
                contents.append({"role": "user", "parts": [{"text": m.content or ""}]})
            elif m.role == "assistant":
                parts = []
                if m.content:
                    parts.append({"text": m.content})
                # for tc in m.tool_calls:
                #     parts.append({"functionCall": {"name": tc.name, "args": tc.input}})
                for tc in m.tool_calls:
                      part: dict = {"functionCall": {"name": tc.name, "args": tc.input}}
                      signature = (tc.provider_metadata or {}).get("thought_signature")
                      if signature:
                          part["thoughtSignature"] = signature
                      parts.append(part)
                contents.append({"role": "model", "parts": parts})
            # elif m.role == "tool":
            #     # Gemini matches a function response to its call by NAME,
            #     # not by id -- it never sees the synthetic id we generate
            #     # for Gemini tool calls (see complete() below).
            #     contents.append(
            #         {
            #             "role": "function",
            #             "parts": [{"functionResponse": {"name": m.name or "", "response": _safe_json(m.content)}}],
            #         }
            #     )
            elif m.role == "tool":
                  contents.append(
                      {
                          "role": "user",
                          "parts": [
                    {
                    "functionResponse": {
                        "name": m.name or "",
                        "id": m.tool_call_id,
                        "response": _safe_json(m.content),
                    }
                }
            ],
        }
    )
        return contents

    def _to_wire_tools(self, tools: list[dict] | None) -> list[dict] | None:
        if not tools:
            return None
        return [
            {
                "function_declarations": [
                    {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                    }
                    for t in tools
                ]
            }
        ]

    async def complete(self, system, messages, tools=None, max_tokens=1500) -> AIResponse:
        if not self.api_key:
            raise AIProviderError("No Gemini API key configured (GEMINI_API_KEY).")

        payload: dict = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": self._to_wire_contents(messages),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        wire_tools = self._to_wire_tools(tools)
        if wire_tools:
            payload["tools"] = wire_tools

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(
                    url,
                    headers={"content-type": "application/json", "x-goog-api-key": self.api_key},
                    json=payload,
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AIProviderError(_extract_error(exc)) from exc
            except httpx.HTTPError as exc:
                raise AIProviderError(f"Could not reach Gemini: {exc}") from exc

        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback", {})
            raise AIProviderError(f"Gemini returned no candidates ({feedback}).")

        parts = candidates[0].get("content", {}).get("parts", [])
        text = "\n".join(p["text"] for p in parts if "text" in p).strip()
        tool_calls = []
        for i, p in enumerate(parts):
             if "functionCall" not in p:
                 continue
             metadata = {"thought_signature": p["thoughtSignature"]} if p.get("thoughtSignature") else None
             tool_calls.append(
                 AIToolCall(
                     id=p["functionCall"].get("id") or f"gemini-call-{i}",
                     name=p["functionCall"].get("name", ""),
                     input=p["functionCall"].get("args", {}),
                     provider_metadata=metadata,
           )
             )
        return AIResponse(text=text, tool_calls=tool_calls)

def _safe_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"raw": raw}


def _extract_error(exc: httpx.HTTPStatusError) -> str:
    try:
        detail = exc.response.json().get("error", {}).get("message", exc.response.text)
    except Exception:  # noqa: BLE001
        detail = exc.response.text
    return f"Gemini API error ({exc.response.status_code}): {detail}"
