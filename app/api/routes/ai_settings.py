"""AI provider settings — lets the frontend see and change which AI
provider/model is active (app/ai/settings_manager.py) and whether the AI
Analysis page's scenario narrative is AI-enriched at all. Runtime-only,
same as trading mode: resets to the .env default on server restart."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.ai.factory import AVAILABLE_MODELS, DEFAULT_MODELS, has_api_key_for
from app.ai.settings_manager import VALID_PROVIDERS, ai_settings_manager

router = APIRouter()


class AISettingsUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    analysis_enrichment_enabled: bool | None = None


def _serialize():
    state = ai_settings_manager.state
    return {
        "provider": state.provider,
        "model": state.model,  # None -> using the provider's own default
        "effective_model": state.model or DEFAULT_MODELS.get(state.provider),
        "analysis_enrichment_enabled": state.analysis_enrichment_enabled,
        "has_api_key": has_api_key_for(state.provider),
        "available_providers": [
            {
                "id": p,
                "default_model": DEFAULT_MODELS[p],
                "available_models": AVAILABLE_MODELS[p],
                "has_api_key": has_api_key_for(p),
            }
            for p in VALID_PROVIDERS
        ],
    }


@router.get("")
async def get_ai_settings():
    return _serialize()


@router.put("")
async def update_ai_settings(update: AISettingsUpdate):
    if update.provider is not None:
        try:
            ai_settings_manager.set_provider(update.provider, update.model)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif update.model is not None:
        # Model-only change: keep the current provider.
        ai_settings_manager.set_provider(ai_settings_manager.state.provider, update.model)

    if update.analysis_enrichment_enabled is not None:
        ai_settings_manager.set_analysis_enrichment(update.analysis_enrichment_enabled)

    return _serialize()
