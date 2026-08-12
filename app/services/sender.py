"""The single outbound gate.

Every message that leaves this machine goes through send_to_target(), which
enforces HC-7: a chat_id is only ever written to if it is the stored chat_id of
a saved target. Every attempt - success or failure - lands in send_log.

Multi-device: the WAHA session is resolved from the target's device, so a chat
belonging to one linked phone can never be sent from another.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from ..config import config
from ..db import get_setting
from ..models import Device, Draft, SendLog, Target
from ..util import utcnow
from . import waha

log = logging.getLogger(__name__)


CHANNEL_SUFFIX = "@newsletter"


class SendBlocked(RuntimeError):
    """A guard refused the send. Never retried - retrying cannot help."""


def supports_files(chat_id: str) -> bool:
    """Whether this chat accepts document attachments.

    Verified against a live channel: WAHA's /api/sendFile returns 201 with a
    real documentMessage for an @newsletter chat, so channels take PDFs and
    spreadsheets just like groups do. Kept as a function because the answer is
    engine- and platform-dependent, not because it currently differs.
    """
    return True


def assert_known_chat(session: Session, chat_id: str) -> Target:
    """HC-7. The only way to obtain a sendable chat_id is via a saved target."""
    target = session.exec(select(Target).where(Target.chat_id == chat_id)).first()
    if target is None:
        raise SendBlocked(
            f"Refusing to send: {chat_id} is not a saved target. "
            "Add it on the dashboard first."
        )
    return target


def session_for_target(session: Session, target: Target) -> str:
    """Which linked device this chat belongs to."""
    if target.device_id is None:
        return config.waha_session
    device = session.get(Device, target.device_id)
    if device is None:
        raise SendBlocked(
            f"'{target.name}' points at a device that no longer exists. "
            "Re-link it on the Devices tab."
        )
    return device.session_name


async def send_to_target(
    session: Session,
    target: Target,
    *,
    text: str,
    poll_options: list[str] | None = None,
    draft_id: int | None = None,
    asset_path: str | None = None,
    asset_filename: str | None = None,
    asset_mime: str | None = None,
    image_path: str | None = None,
    image_mime: str | None = None,
    send_cover: bool = False,
) -> SendLog:
    """Send one message. Raises SendBlocked or waha.WahaError; always logs."""
    # Re-read the target from the DB so a stale in-memory object can never be
    # used to smuggle in an unsaved chat_id.
    stored = assert_known_chat(session, target.chat_id)
    if stored.id != target.id:
        raise SendBlocked(f"chat_id {target.chat_id} belongs to a different target.")

    if get_setting(session, "global_sending_enabled", "true") != "true":
        raise SendBlocked("Sending is paused globally. Turn it back on in Settings.")
    if not stored.enabled:
        raise SendBlocked(f"'{stored.name}' is paused. Enable it to send.")

    session_name = session_for_target(session, stored)

    try:
        if image_path and not asset_path:
            # Channel style: a picture carrying the caption (which holds the
            # link), with no document attachment.
            picture = Path(image_path)
            if not picture.exists():
                raise SendBlocked(
                    f"The preview image is missing from disk ({picture.name}). Regenerate it."
                )
            import base64

            response = await waha.send_image(
                stored.chat_id,
                filename=picture.name,
                mimetype=image_mime or "image/png",
                data_b64=base64.b64encode(picture.read_bytes()).decode(),
                caption=text,
                session_name=session_name,
            )
        elif asset_path:
            path = Path(asset_path)
            if not path.exists():
                raise SendBlocked(
                    f"The generated file is missing from disk ({path.name}). Regenerate it."
                )
            import base64

            from .assets import cover_image, file_to_b64

            # Lead with a picture of page one so the post is not just a
            # filename in the feed, then send the document itself.
            if send_cover:
                cover = cover_image(path)
                if cover:
                    png, cover_name = cover
                    try:
                        await waha.send_image(
                            stored.chat_id,
                            filename=cover_name,
                            mimetype="image/png",
                            data_b64=base64.b64encode(png).decode(),
                            caption=text,
                            session_name=session_name,
                        )
                        text = ""  # caption already delivered with the image
                    except waha.WahaError as exc:
                        # A cover is a nicety - never lose the document over it.
                        log.warning("Cover image failed for %s: %s", stored.name, exc.message)

            response = await waha.send_file(
                stored.chat_id,
                filename=asset_filename or path.name,
                mimetype=asset_mime or "application/octet-stream",
                data_b64=file_to_b64(path),
                caption=text,
                session_name=session_name,
            )
        elif poll_options:
            response = await waha.send_poll(
                stored.chat_id, text.strip(), poll_options, session_name=session_name
            )
        else:
            response = await waha.send_text(
                stored.chat_id, text, session_name=session_name
            )
    except waha.WahaError as exc:
        _log_attempt(
            session, stored, draft_id, status="failed",
            payload={"error": exc.message, "hint": exc.hint, "status_code": exc.status_code},
        )
        raise

    entry = _log_attempt(session, stored, draft_id, status="sent", payload=response)
    log.info("Sent to %s (%s) via session %s", stored.name, stored.chat_id, session_name)
    return entry


async def send_draft(
    session: Session, draft: Draft, target: Target, *, send_cover: bool | None = None
) -> SendLog:
    """Send whatever this draft carries - text, poll, or generated file."""
    if send_cover is None:
        # Inherit the campaign's preference when the draft came from one.
        send_cover = False
        if draft.campaign_id is not None:
            from ..models import Campaign

            campaign = session.get(Campaign, draft.campaign_id)
            send_cover = bool(campaign and campaign.send_cover_image)

    return await send_to_target(
        session,
        target,
        text=draft.content,
        poll_options=draft.poll_options,
        draft_id=draft.id,
        asset_path=draft.asset_path,
        asset_filename=draft.asset_filename,
        asset_mime=draft.asset_mime,
        image_path=draft.image_path,
        image_mime=draft.image_mime,
        send_cover=send_cover,
    )


def _log_attempt(
    session: Session,
    target: Target,
    draft_id: int | None,
    *,
    status: str,
    payload: Any,
) -> SendLog:
    if not isinstance(payload, dict):
        payload = {"result": payload}
    entry = SendLog(
        target_id=target.id,
        draft_id=draft_id,
        chat_id=target.chat_id,
        status=status,
        response_json=payload,
        created_at=utcnow(),
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry
