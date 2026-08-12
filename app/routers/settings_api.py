"""App settings: default free model, global toggles, free-tier usage counter."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ..config import config, is_free_model
from ..db import all_settings, get_session, set_setting
from ..services.llm import (
    LLMError,
    calls_today,
    key_source,
    mask_key,
    resolve_api_key,
    set_api_key,
    verify_api_key,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# OpenRouter's published ceiling for a $0 account. Shown as a soft warning only.
FREE_DAILY_LIMIT = 50


class SettingsPatch(BaseModel):
    default_model: str | None = None
    global_sending_enabled: bool | None = None
    # "" clears the stored key and falls back to .env (if that has one).
    openrouter_api_key: str | None = None


class KeyTest(BaseModel):
    # Omit to test the key already saved.
    openrouter_api_key: str | None = None


@router.get("")
def read_settings(session: Session = Depends(get_session)) -> dict[str, Any]:
    values = all_settings(session)
    key = resolve_api_key(session)
    return {
        "default_model": values.get("default_model", config.default_model),
        "global_sending_enabled": values.get("global_sending_enabled", "true") == "true",
        "openrouter_ready": bool(key),
        # Never return the key itself - only enough to recognise it.
        "openrouter_key_masked": mask_key(key),
        "openrouter_key_source": key_source(session),
        "timezone": config.timezone,
        "waha_session": config.waha_session,
        "waha_base_url": config.waha_base_url,
        "calls_today": calls_today(session),
        "free_daily_limit": FREE_DAILY_LIMIT,
    }


@router.patch("")
def update_settings(
    patch: SettingsPatch, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if patch.default_model is not None:
        model = patch.default_model.strip()
        if not is_free_model(model):
            # HC-1: refuse anything that could bill me.
            raise HTTPException(
                status_code=400,
                detail=f"'{model}' is not a free model. The id must end with ':free'.",
            )
        set_setting(session, "default_model", model)

    if patch.global_sending_enabled is not None:
        set_setting(
            session,
            "global_sending_enabled",
            "true" if patch.global_sending_enabled else "false",
        )

    if patch.openrouter_api_key is not None:
        key = patch.openrouter_api_key.strip()
        if key and not key.startswith("sk-or-"):
            raise HTTPException(
                status_code=400,
                detail="That does not look like an OpenRouter key — they start with 'sk-or-'.",
            )
        set_api_key(session, key)

    return read_settings(session)


@router.post("/test-key")
async def test_key(
    payload: KeyTest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Validate a key against OpenRouter before (or after) saving it."""
    key = (payload.openrouter_api_key or "").strip() or resolve_api_key(session)
    if not key:
        raise HTTPException(status_code=400, detail="No API key to test.")
    try:
        info = await verify_api_key(key)
    except LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **info}


@router.get("/models")
async def list_free_models() -> dict[str, Any]:
    """Live model list from OpenRouter, filtered to ids ending ':free'.

    This endpoint is public on OpenRouter's side, so the dropdown populates even
    before an API key is configured.
    """
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(OPENROUTER_MODELS_URL)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not reach OpenRouter: {exc}"
        ) from exc

    models = []
    for item in payload.get("data", []):
        model_id = item.get("id", "")
        if not is_free_model(model_id):
            continue
        models.append(
            {
                "id": model_id,
                "name": item.get("name") or model_id,
                "context_length": item.get("context_length"),
            }
        )
    models.sort(key=lambda m: m["name"].lower())
    return {"count": len(models), "models": models}
