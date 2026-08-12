"""Telegram Bot API client.

Deliberately mirrors services/waha.py so the sender can treat both platforms
the same way.

The important difference from WhatsApp: **a bot cannot list the chats it
belongs to.** There is no getChats. So groups are discovered two ways:

  * getUpdates - Telegram queues recent updates, including the `my_chat_member`
    event fired when the bot is added to a group. Reading those surfaces
    anything that has spoken to, or added, the bot in the last ~24h.
  * manual add - paste an @username or a numeric id and getChat resolves it.

Formatting differs too: WhatsApp wants *bold*, Telegram wants HTML or
MarkdownV2. Messages are converted on the way out.
"""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=120.0, pool=5.0)

# Telegram's own ceilings.
MAX_TEXT = 4096
MAX_CAPTION = 1024


class TelegramError(RuntimeError):
    """Any failure talking to Telegram. Message is safe to show in the UI."""

    def __init__(self, message: str, *, status_code: int | None = None, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.hint = hint


async def _call(
    token: str, method: str, *, data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
) -> Any:
    if not token:
        raise TelegramError("No bot token set for this device.")

    url = f"{API_ROOT}/bot{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            if files:
                resp = await client.post(url, data=data or {}, files=files)
            else:
                resp = await client.post(url, json=data or {})
    except httpx.HTTPError as exc:
        raise TelegramError(f"Could not reach Telegram: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise TelegramError(f"Telegram returned a non-JSON response ({resp.status_code}).") from exc

    if not payload.get("ok"):
        description = str(payload.get("description") or "Unknown error")
        raise TelegramError(
            description, status_code=resp.status_code, hint=_hint_for(description)
        )
    return payload.get("result")


def _hint_for(description: str) -> str:
    """Turn Telegram's terse errors into something actionable."""
    lowered = description.lower()
    if "unauthorized" in lowered:
        return "The bot token is wrong or was revoked. Get a fresh one from @BotFather."
    if "chat not found" in lowered:
        return ("Add the bot to that group first, and make it an admin. Bots cannot "
                "post to a chat they have not joined.")
    if "not enough rights" in lowered or "administrator" in lowered:
        return "Make the bot an admin of the group, with permission to post."
    if "bot was kicked" in lowered or "bot is not a member" in lowered:
        return "The bot was removed from that chat. Add it back as an admin."
    if "too many requests" in lowered or "retry after" in lowered:
        return "Telegram rate limit. Wait a moment and try again."
    return ""


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


async def get_me(token: str) -> dict[str, Any]:
    """Who this token belongs to. Used to validate a newly pasted token."""
    return await _call(token, "getMe")


async def check(token: str) -> dict[str, Any]:
    """Never raises - the Devices page must stay renderable."""
    try:
        me = await get_me(token)
    except TelegramError as exc:
        return {"ok": False, "status": "FAILED", "error": exc.message, "hint": exc.hint}
    return {
        "ok": True,
        "status": "WORKING",
        "username": me.get("username", ""),
        "name": me.get("first_name", ""),
        "id": me.get("id"),
        "error": None,
        "hint": "",
    }


# ---------------------------------------------------------------------------
# Chat discovery
# ---------------------------------------------------------------------------

_CHAT_TYPES = {"group": "group", "supergroup": "supergroup", "channel": "channel"}


def _normalise_chat(chat: dict[str, Any]) -> dict[str, Any] | None:
    kind = chat.get("type")
    if kind not in _CHAT_TYPES:
        return None  # skip private DMs - this app posts to communities
    title = chat.get("title") or chat.get("username") or str(chat.get("id"))
    return {
        "id": str(chat.get("id")),
        "name": title,
        "participants": None,
        "is_community": kind == "supergroup",
        "is_announce": kind == "channel",
        "chat_type": _CHAT_TYPES[kind],
    }


async def discover_chats(token: str) -> list[dict[str, Any]]:
    """Groups and channels visible in the bot's pending updates.

    getUpdates is read without an offset so the queue is not consumed - polling
    it here must not eat updates the user might rely on elsewhere. Telegram only
    keeps ~24h of updates, so this shows recent activity, not a full list. That
    is a Bot API limitation, not something the app can work around.
    """
    try:
        updates = await _call(token, "getUpdates", data={"timeout": 0, "limit": 100})
    except TelegramError as exc:
        if "webhook" in exc.message.lower():
            raise TelegramError(
                "This bot has a webhook set, so pending updates cannot be read. "
                "Add chats manually by @username or id instead.",
                hint=exc.hint,
            ) from exc
        raise

    found: dict[str, dict[str, Any]] = {}
    for update in updates or []:
        for key in ("message", "channel_post", "edited_message", "my_chat_member",
                    "chat_member", "callback_query"):
            node = update.get(key)
            if not isinstance(node, dict):
                continue
            chat = node.get("chat") or (node.get("message") or {}).get("chat")
            if isinstance(chat, dict):
                row = _normalise_chat(chat)
                if row:
                    found[row["id"]] = row

    return sorted(found.values(), key=lambda c: c["name"].lower())


async def resolve_chat(token: str, handle: str) -> dict[str, Any]:
    """Look up one chat by @username or numeric id."""
    handle = handle.strip()
    if not handle:
        raise TelegramError("Enter a @username or a chat id.")
    if handle.startswith("https://t.me/"):
        handle = "@" + handle.rsplit("/", 1)[-1]
    if not handle.startswith("@") and not re.fullmatch(r"-?\d+", handle):
        handle = "@" + handle

    chat = await _call(token, "getChat", data={"chat_id": handle})
    row = _normalise_chat(chat)
    if row is None:
        raise TelegramError(
            f"'{handle}' is a {chat.get('type')} chat. Only groups, supergroups "
            "and channels can be used here."
        )
    return row


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def to_html(text: str) -> str:
    """WhatsApp-style markup -> Telegram HTML.

    The drafts are written for WhatsApp (*bold*, _italic_). Telegram renders
    those literally, so they are converted here and everything else is escaped
    first so a stray < in the copy cannot break the message.
    """
    escaped = html.escape(text or "", quote=False)
    escaped = re.sub(r"(?<!\w)\*(\S(?:[^*\n]*\S)?)\*(?!\w)", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\w)_(\S(?:[^_\n]*\S)?)_(?!\w)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"(?<!\w)~(\S(?:[^~\n]*\S)?)~(?!\w)", r"<s>\1</s>", escaped)
    return escaped


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    boundary = cut.rfind("\n")
    if boundary < limit * 0.6:
        boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > 0 else cut).rstrip() + "…"


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


async def send_text(token: str, chat_id: str, text: str) -> dict[str, Any]:
    return await _call(token, "sendMessage", data={
        "chat_id": chat_id,
        "text": _clip(to_html(text), MAX_TEXT),
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": False},
    })


async def send_photo(
    token: str, chat_id: str, path: Path, caption: str = ""
) -> dict[str, Any]:
    with path.open("rb") as handle:
        return await _call(
            token, "sendPhoto",
            data={"chat_id": chat_id,
                  "caption": _clip(to_html(caption), MAX_CAPTION),
                  "parse_mode": "HTML"},
            files={"photo": (path.name, handle)},
        )


async def send_document(
    token: str, chat_id: str, path: Path, caption: str = "", filename: str | None = None
) -> dict[str, Any]:
    with path.open("rb") as handle:
        return await _call(
            token, "sendDocument",
            data={"chat_id": chat_id,
                  "caption": _clip(to_html(caption), MAX_CAPTION),
                  "parse_mode": "HTML"},
            files={"document": (filename or path.name, handle)},
        )


async def send_poll(
    token: str, chat_id: str, question: str, options: list[str]
) -> dict[str, Any]:
    # Telegram takes plain text for polls - no HTML - and caps the lengths.
    return await _call(token, "sendPoll", data={
        "chat_id": chat_id,
        "question": question[:300],
        "options": [o[:100] for o in options[:10]],
        "is_anonymous": True,
    })
