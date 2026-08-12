"""One interface over both generation engines.

  * openrouter  - free `:free` models. Fast, no subscription usage, ~50/day,
                  and prone to upstream rate limits (handled by fallback).
  * claude_code - the local Claude Code CLI on the Max plan. Slower, no
                  per-call cost, no daily cap, noticeably better on long
                  structured output like a full PDF outline.

Both are always available; the caller picks per campaign.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from sqlmodel import Session

from ..models import Engine
from . import claude_engine, llm

log = logging.getLogger(__name__)


class EngineError(RuntimeError):
    """Generation failed. Message is safe to show in the UI."""


def _messages_to_prompt(messages: list[dict[str, str]]) -> tuple[str, str]:
    """Collapse a chat-style message list into (system, user) for the CLI."""
    system_parts = [m["content"] for m in messages if m["role"] == "system"]

    conversation: list[str] = []
    for message in messages:
        if message["role"] == "system":
            continue
        if message["role"] == "assistant":
            conversation.append(f"EXAMPLE OF THE STYLE I WANT:\n{message['content']}")
        else:
            conversation.append(message["content"])

    return "\n\n".join(system_parts), "\n\n".join(conversation)


async def generate(
    session: Session,
    engine: Engine,
    messages: list[dict[str, str]],
    *,
    model_override: str | None = None,
    timeout: int = 300,
    on_note: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """Run one generation. Returns (text, engine_label)."""
    if engine == Engine.claude_code:
        system, prompt = _messages_to_prompt(messages)
        try:
            text = await claude_engine.generate(
                prompt, system=system, model=model_override or None, timeout=timeout
            )
        except claude_engine.ClaudeCodeError as exc:
            raise EngineError(str(exc)) from exc
        return text, f"claude-code{'/' + model_override if model_override else ''}"

    # OpenRouter
    api_key = llm.resolve_api_key(session)
    model = llm.resolve_model(session, model_override)
    try:
        text, model_used = await llm.chat_with_fallback(
            model, messages, api_key=api_key, on_note=on_note
        )
    except llm.LLMError as exc:
        raise EngineError(str(exc)) from exc
    finally:
        llm.record_call(session)
    return text, model_used


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def extract_json(raw: str) -> dict[str, Any]:
    """Pull a JSON object out of a model reply.

    Models wrap JSON in prose or code fences, and smaller ones emit trailing
    commas. Try progressively more forgiving strategies rather than failing
    the whole generation on a cosmetic flaw.
    """
    text = (raw or "").strip()

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    candidates = [text]

    # Widest brace span - handles leading/trailing commentary.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        for attempt in (candidate, re.sub(r",(\s*[}\]])", r"\1", candidate)):
            try:
                data = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
            if isinstance(data, list):
                return {"rows": data}

    raise EngineError(
        "The model did not return valid JSON for this asset. Try again, "
        "or switch engine — Claude Code is more reliable for structured output."
    )


async def generate_json(
    session: Session,
    engine: Engine,
    messages: list[dict[str, str]],
    *,
    model_override: str | None = None,
    timeout: int = 300,
    on_note: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], str]:
    """Generate and parse a JSON payload. Returns (data, engine_label)."""
    raw, label = await generate(
        session, engine, messages, model_override=model_override,
        timeout=timeout, on_note=on_note,
    )
    return extract_json(raw), label


async def availability(session: Session) -> dict[str, Any]:
    """What the Composer should offer right now."""
    claude = await claude_engine.check()
    key = llm.resolve_api_key(session)
    return {
        "openrouter": {
            "available": bool(key),
            "detail": "Ready" if key else "No API key — add one in Settings.",
            "calls_today": llm.calls_today(session),
        },
        "claude_code": {
            "available": bool(claude.get("available") and claude.get("logged_in")),
            "detail": (
                f"Ready ({claude.get('version', '')})"
                if claude.get("logged_in")
                else (claude.get("error") or "Unavailable")
            ),
            "hint": claude.get("hint", ""),
        },
    }
