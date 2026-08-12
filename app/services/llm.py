"""OpenRouter transport.

HC-1: only models whose id ends in ':free' are ever called. Anything else is
refused before the request leaves the machine, so a typo can never bill me.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

import httpx
from sqlmodel import Session

from typing import Any

from ..config import config, is_free_model
from ..db import get_setting, set_setting

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=5.0)

# Where the key lives when set from the Settings page.
API_KEY_SETTING = "openrouter_api_key"


class LLMError(RuntimeError):
    """Any failure drafting a message. Message is safe to show in the UI.

    `retryable` means "this model is the problem, another one may work" - an
    upstream rate limit or a model that returns no usable text. A bad API key
    is not retryable, so we never burn the fallback list on it.
    """

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# Ordered fallback list, verified against the live free tier: each of these
# returns clean WhatsApp copy in `content`. Several other :free models are
# reasoning-tuned and return an empty `content` with the text stranded in a
# `reasoning` field, which is useless here - they are deliberately excluded.
FALLBACK_MODELS = [
    "google/gemma-4-26b-a4b-it:free",
    "poolside/laguna-s-2.1:free",
    "google/gemma-4-31b-it:free",
    "inclusionai/ling-3.0-tiny:free",
]


# ---------------------------------------------------------------------------
# API key resolution
#
# Settings-page value wins over .env, so the key can be managed entirely from
# the dashboard. Either way it is only ever read from git-ignored storage.
# ---------------------------------------------------------------------------


def resolve_api_key(session: Session) -> str:
    stored = get_setting(session, API_KEY_SETTING, "").strip()
    if stored:
        return stored
    return config.openrouter_api_key if config.openrouter_ready else ""


def key_source(session: Session) -> str:
    if get_setting(session, API_KEY_SETTING, "").strip():
        return "settings"
    return "env" if config.openrouter_ready else "none"


def mask_key(key: str) -> str:
    """Show just enough to recognise which key is saved."""
    if not key:
        return ""
    if len(key) <= 12:
        return "•" * len(key)
    return f"{key[:10]}…{key[-4:]}"


def set_api_key(session: Session, key: str) -> None:
    set_setting(session, API_KEY_SETTING, key.strip())


async def verify_api_key(key: str) -> dict[str, Any]:
    """Ask OpenRouter whether this key is real, and what its limits are."""
    if not key.strip():
        raise LLMError("No API key provided.")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {key.strip()}"}
            )
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach OpenRouter: {exc}") from exc

    if resp.status_code in (401, 403):
        raise LLMError("OpenRouter rejected this key. Check you copied all of it.")
    if resp.status_code >= 400:
        raise LLMError(f"OpenRouter returned {resp.status_code}: {_short(resp)}")

    data = resp.json().get("data", {}) if resp.content else {}
    return {
        "label": data.get("label") or "",
        "usage": data.get("usage"),
        "limit": data.get("limit"),
        "is_free_tier": data.get("is_free_tier"),
    }


# ---------------------------------------------------------------------------
# Free-tier usage counter (roughly 50 requests/day on a $0 account)
# ---------------------------------------------------------------------------


def _counter_key(day: date | None = None) -> str:
    return f"openrouter_calls_{(day or date.today()).isoformat()}"


def calls_today(session: Session) -> int:
    try:
        return int(get_setting(session, _counter_key(), "0"))
    except ValueError:
        return 0


def record_call(session: Session) -> int:
    count = calls_today(session) + 1
    set_setting(session, _counter_key(), str(count))
    return count


# ---------------------------------------------------------------------------
# Chat completion
# ---------------------------------------------------------------------------


async def chat(
    model: str,
    messages: list[dict[str, str]],
    *,
    api_key: str,
    temperature: float = 0.8,
    max_tokens: int = 900,
) -> str:
    """One completion call. Returns the assistant text."""
    if not is_free_model(model):
        raise LLMError(
            f"Refusing to call '{model}': only models ending in ':free' are allowed."
        )
    if not api_key:
        raise LLMError(
            "No OpenRouter API key set. Add one on the Settings page — "
            "create a free key at https://openrouter.ai/keys."
        )

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Optional attribution headers; harmless and helps OpenRouter's dashboard.
        "HTTP-Referer": "http://localhost:8080",
        "X-Title": "WhatsApp Community Manager",
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach OpenRouter: {exc}") from exc

    if resp.status_code == 401:
        raise LLMError(
            "OpenRouter rejected the API key (401). Re-check the key on the Settings page."
        )
    if resp.status_code == 429:
        # Two very different situations share this code: one free model being
        # rate-limited at the provider (transient - another model will work)
        # versus this account's own daily quota (switching model will not help).
        detail = _short(resp)
        lowered = detail.lower()
        account_limit = any(
            hint in lowered
            for hint in ("daily limit", "free-tier limit", "requests per day",
                         "add credits", "add more credits", "quota")
        )
        if account_limit:
            raise LLMError(
                "OpenRouter daily free-tier limit reached (roughly 50 requests/day "
                f"on a free account). {detail}",
                retryable=False,
            )
        # Default to retryable: assuming the model is at fault costs one extra
        # attempt, while wrongly assuming the account is capped loses the message.
        raise LLMError(
            f"'{model}' is rate-limited at the provider right now. {detail}",
            retryable=True,
        )
    if resp.status_code in (502, 503):
        raise LLMError(
            f"'{model}' is temporarily unavailable ({resp.status_code}).", retryable=True
        )
    if resp.status_code >= 400:
        raise LLMError(f"OpenRouter returned {resp.status_code}: {_short(resp)}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise LLMError("OpenRouter returned a non-JSON response.") from exc

    # Some free models surface upstream failures inside a 200 body.
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise LLMError(f"OpenRouter error: {message}")

    choices = data.get("choices") or []
    if not choices:
        raise LLMError(
            f"'{model}' returned no choices - it may be overloaded.", retryable=True
        )

    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        # Reasoning-tuned models often put everything in `reasoning` and leave
        # `content` empty. That text is chain-of-thought, not a message, so it
        # must never be published - move on to a model that answers properly.
        if message.get("reasoning"):
            raise LLMError(
                f"'{model}' is a reasoning model and returned no message text, "
                "only internal reasoning. Trying a different model.",
                retryable=True,
            )
        raise LLMError(f"'{model}' returned an empty message.", retryable=True)
    return content


async def chat_with_fallback(
    preferred: str,
    messages: list[dict[str, str]],
    *,
    api_key: str,
    on_note: Callable[[str], None] | None = None,
    **kwargs: Any,
) -> tuple[str, str]:
    """Try the preferred model, then known-good free ones.

    Free models flap constantly - one being rate-limited upstream should not
    cost you the day's message. `on_note` reports each switch so the UI can
    show why a generation is taking longer. Returns (text, model_used).
    """
    candidates = [preferred] + [m for m in FALLBACK_MODELS if m != preferred]
    problems: list[str] = []

    for index, candidate in enumerate(candidates):
        if on_note and index:
            on_note(f"{candidate.split('/')[-1]} — attempt {index + 1}")
        try:
            text = await chat(candidate, messages, api_key=api_key, **kwargs)
        except LLMError as exc:
            if not exc.retryable:
                raise
            log.warning("Model %s unusable: %s", candidate, exc)
            problems.append(f"{candidate}: {exc}")
            if on_note:
                on_note(f"{candidate.split('/')[-1]} unavailable, trying another model…")
            continue
        if candidate != preferred:
            log.info("Fell back to %s (preferred %s was unavailable)", candidate, preferred)
        return text, candidate

    raise LLMError(
        "Every free model refused this request. "
        + " | ".join(problems[:3])
    )


def _short(resp: httpx.Response, limit: int = 300) -> str:
    """Best available explanation from an OpenRouter error body.

    `error.message` is often just "Provider returned error"; the useful text
    (e.g. "temporarily rate-limited upstream") lives in error.metadata.raw.
    """
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:limit]

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            parts: list[str] = []
            metadata = err.get("metadata")
            if isinstance(metadata, dict) and metadata.get("raw"):
                parts.append(str(metadata["raw"]))
            if err.get("message"):
                parts.append(str(err["message"]))
            if parts:
                # Longest first: the raw upstream text is the informative one.
                return max(parts, key=len)[:limit]
        for key in ("message", "detail"):
            if key in data:
                return str(data[key])[:limit]
    return str(data)[:limit]


def resolve_model(session: Session, override: str | None) -> str:
    """Target override wins, else the app-wide default. Always validated."""
    model = (override or "").strip() or get_setting(
        session, "default_model", config.default_model
    )
    if not is_free_model(model):
        raise LLMError(
            f"Configured model '{model}' is not free. Pick one ending in ':free' in Settings."
        )
    return model
