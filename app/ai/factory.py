"""Builds the currently-selected AIProvider (see settings_manager.py for
what "currently selected" means) and lists what's available for the
frontend's provider/model picker."""
from app.ai.anthropic_provider import DEFAULT_MODEL as ANTHROPIC_DEFAULT_MODEL
from app.ai.anthropic_provider import AnthropicProvider
from app.ai.base import AIProvider
from app.ai.gemini_provider import DEFAULT_MODEL as GEMINI_DEFAULT_MODEL
from app.ai.gemini_provider import GeminiProvider
from app.ai.openai_provider import DEFAULT_MODEL as OPENAI_DEFAULT_MODEL
from app.ai.openai_provider import OpenAIProvider
from app.ai.settings_manager import ai_settings_manager
from app.config import get_settings

DEFAULT_MODELS = {
    "anthropic": ANTHROPIC_DEFAULT_MODEL,
    "gemini": GEMINI_DEFAULT_MODEL,
    "openai": OPENAI_DEFAULT_MODEL,
}

# A starting point for the frontend dropdown, NOT a guarantee these exact
# strings are current on every provider's side -- model names change
# often; verify against each provider's docs if a call fails with an
# "unknown model" error.
AVAILABLE_MODELS = {
    "anthropic": ["claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5-20251001", "claude-fable-5"],
    "gemini": ["gemini-3.5-flash-lite", "gemini-3.7-flash", "gemini-3.1-pro-preview"],
    "openai": ["gpt-4.1", "gpt-4.1-mini", "gpt-4o"],
}


def get_current_provider() -> AIProvider:
    settings = get_settings()
    state = ai_settings_manager.state
    model = state.model

    if state.provider == "anthropic":
        return AnthropicProvider(api_key=settings.anthropic_api_key, model=model)
    if state.provider == "gemini":
        return GeminiProvider(api_key=settings.gemini_api_key, model=model)
    if state.provider == "openai":
        return OpenAIProvider(api_key=settings.openai_api_key, model=model)
    raise ValueError(f"Unknown provider '{state.provider}'.")


def has_api_key_for(provider: str) -> bool:
    settings = get_settings()
    return bool(
        {
            "anthropic": settings.anthropic_api_key,
            "gemini": settings.gemini_api_key,
            "openai": settings.openai_api_key,
        }.get(provider)
    )
