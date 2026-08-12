"""Live WAHA proxies: session status and the group/channel picker."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..db import get_session
from ..models import Device, Platform, Target
from ..services import waha

router = APIRouter(prefix="/api/waha", tags=["waha"])


def _session_name(session: Session, device_id: int | None) -> str | None:
    if device_id is None:
        return None
    device = session.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    return device.session_name


@router.get("/status")
async def status(
    device_id: int | None = None, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Never 500s - the UI needs a renderable answer even when WAHA is down."""
    return await waha.session_status(_session_name(session, device_id))


@router.post("/session/start")
async def start_session(
    device_id: int | None = None, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        result = await waha.start_session(_session_name(session, device_id))
    except waha.WahaError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    return {"ok": True, "result": result}


def _mark_saved(rows: list[dict[str, Any]], saved: set[str]) -> list[dict[str, Any]]:
    for row in rows:
        row["already_added"] = row["id"] in saved
    return rows


@router.get("/groups")
async def groups(
    device_id: int | None = None, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    try:
        rows = await waha.list_groups(_session_name(session, device_id))
    except waha.WahaError as exc:
        raise HTTPException(status_code=502, detail=f"{exc.message} {exc.hint}".strip()) from exc
    saved = {t.chat_id for t in session.exec(select(Target)).all()}
    return _mark_saved(rows, saved)


@router.get("/channels")
async def channels(
    device_id: int | None = None, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    try:
        rows = await waha.list_channels(_session_name(session, device_id))
    except waha.WahaError as exc:
        raise HTTPException(status_code=502, detail=f"{exc.message} {exc.hint}".strip()) from exc
    saved = {t.chat_id for t in session.exec(select(Target)).all()}
    return _mark_saved(rows, saved)


@router.get("/chats")
async def all_chats(
    device_id: int | None = None, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Groups and channels in one call - what the Composer picker needs."""
    saved = {t.chat_id for t in session.exec(select(Target)).all()}

    device = session.get(Device, device_id) if device_id is not None else None
    if device is not None and device.platform == Platform.telegram:
        from ..services import telegram

        result: dict[str, Any] = {"groups": [], "channels": [], "errors": [],
                                  "platform": "telegram"}
        try:
            chats = await telegram.discover_chats(device.bot_token)
        except telegram.TelegramError as exc:
            result["errors"].append(f"{exc.message} {exc.hint}".strip())
            return result

        for chat in _mark_saved(chats, saved):
            bucket = "channels" if chat["chat_type"] == "channel" else "groups"
            result[bucket].append(chat)

        if not chats:
            result["errors"].append(
                "Telegram bots cannot list their groups. Send any message in the "
                "group (or re-add the bot) so it appears here, or add it by "
                "@username on the Devices tab."
            )
        return result

    name = _session_name(session, device_id)

    result = {"groups": [], "channels": [], "errors": [], "platform": "whatsapp"}
    try:
        result["groups"] = _mark_saved(await waha.list_groups(name), saved)
    except waha.WahaError as exc:
        result["errors"].append(f"Groups: {exc.message} {exc.hint}".strip())
    try:
        result["channels"] = _mark_saved(await waha.list_channels(name), saved)
    except waha.WahaError as exc:
        result["errors"].append(f"Channels: {exc.message} {exc.hint}".strip())
    return result
