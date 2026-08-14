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
from ..models import Device, Draft, Platform, SendLog, Target
from ..util import utcnow
from . import telegram, waha

log = logging.getLogger(__name__)


CHANNEL_SUFFIX = "@newsletter"


def is_channel(chat_id: str) -> bool:
    return (chat_id or "").endswith(CHANNEL_SUFFIX)


class SendBlocked(RuntimeError):
    """A guard refused the send. Never retried - retrying cannot help."""


class AwaitingApproval(SendBlocked):
    """Refused because sign-off is still outstanding.

    A SendBlocked subclass so every existing handler still catches it and
    nothing escapes as a 500, but its own type so callers that care can leave
    the draft pending instead of marking it failed - it has not failed, it is
    simply not approved yet.
    """


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


def device_for_target(session: Session, target: Target) -> Device | None:
    """The linked account this chat belongs to."""
    if target.device_id is None:
        return None
    device = session.get(Device, target.device_id)
    if device is None:
        raise SendBlocked(
            f"'{target.name}' points at a device that no longer exists. "
            "Re-link it on the Devices tab."
        )
    return device


def session_for_target(session: Session, target: Target) -> str:
    """WAHA session name for a WhatsApp target."""
    device = device_for_target(session, target)
    return device.session_name if device else config.waha_session


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

    device = device_for_target(session, stored)

    # Telegram is a different transport entirely - bot token, HTTP file upload,
    # HTML markup - so it branches here rather than pretending to be WAHA.
    if device is not None and device.platform == Platform.telegram:
        return await _send_telegram(
            session, stored, device,
            text=text, poll_options=poll_options, draft_id=draft_id,
            asset_path=asset_path, asset_filename=asset_filename,
            image_path=image_path, send_cover=send_cover,
        )

    session_name = device.session_name if device else config.waha_session

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
                        cover_response = await waha.send_image(
                            stored.chat_id,
                            filename=cover_name,
                            mimetype="image/png",
                            data_b64=base64.b64encode(png).decode(),
                            caption=text,
                            session_name=session_name,
                        )
                        # This message reached the chat, so it must appear in
                        # the log. It used to be invisible: if the document
                        # then failed, the draft was marked failed and the log
                        # showed nothing, while the caption was already posted.
                        _log_attempt(
                            session, stored, draft_id, status="sent",
                            payload={"part": "cover", **(cover_response or {})},
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
            # Channels carry a link in almost every post, and WhatsApp scrapes
            # it into whatever card the destination exposes - a Drive
            # spreadsheet link renders as a blurry "Loading Google Sheets"
            # tile. Suppressing the preview keeps the post as written; the link
            # is still tappable.
            response = await waha.send_text(
                stored.chat_id, text, session_name=session_name,
                link_preview=not is_channel(stored.chat_id),
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


async def _send_telegram(
    session: Session,
    target: Target,
    device: Device,
    *,
    text: str,
    poll_options: list[str] | None,
    draft_id: int | None,
    asset_path: str | None,
    asset_filename: str | None,
    image_path: str | None,
    send_cover: bool,
) -> SendLog:
    """Same contract as the WhatsApp path: send, log, raise on failure."""
    token = device.bot_token
    if not token:
        raise SendBlocked(
            f"'{device.name}' has no bot token. Re-add it on the Devices tab."
        )

    try:
        if poll_options:
            response = await telegram.send_poll(
                token, target.chat_id, text.strip(), poll_options
            )
        elif asset_path:
            path = Path(asset_path)
            if not path.exists():
                raise SendBlocked(
                    f"The generated file is missing from disk ({path.name}). Regenerate it."
                )
            # Telegram renders a document card with a thumbnail, so a separate
            # cover image is only worth sending when it carries the caption.
            if send_cover and image_path and Path(image_path).exists():
                try:
                    await telegram.send_photo(
                        token, target.chat_id, Path(image_path), caption=text
                    )
                    text = ""
                except telegram.TelegramError as exc:
                    log.warning("Telegram cover failed for %s: %s", target.name, exc.message)
            response = await telegram.send_document(
                token, target.chat_id, path, caption=text,
                filename=asset_filename or path.name,
            )
        elif image_path:
            picture = Path(image_path)
            if not picture.exists():
                raise SendBlocked(f"The preview image is missing ({picture.name}).")
            response = await telegram.send_photo(
                token, target.chat_id, picture, caption=text
            )
        else:
            response = await telegram.send_text(token, target.chat_id, text)
    except telegram.TelegramError as exc:
        _log_attempt(
            session, target, draft_id, status="failed",
            payload={"error": exc.message, "hint": exc.hint,
                     "status_code": exc.status_code, "platform": "telegram"},
        )
        # Surfaced to callers as a WahaError so every caller's existing
        # error handling keeps working across both platforms.
        raise waha.WahaError(exc.message, status_code=exc.status_code, hint=exc.hint) from exc

    entry = _log_attempt(session, target, draft_id, status="sent", payload=response)
    log.info("Sent to %s (%s) via Telegram bot %s",
             target.name, target.chat_id, device.session_name)
    return entry


def pending_approval(session: Session, draft: Draft):
    """The unresolved approval covering this draft, if there is one."""
    from ..models import ApprovalRequest, ApprovalStatus

    if draft.campaign_id is None:
        return None
    rows = session.exec(
        select(ApprovalRequest).where(
            ApprovalRequest.campaign_id == draft.campaign_id,
            ApprovalRequest.status == ApprovalStatus.pending,
        )
    ).all()
    for row in rows:
        # target_id None is the old campaign-wide form and covers every draft.
        if row.target_id is None or row.target_id == draft.target_id:
            return row
    return None


async def send_draft(
    session: Session,
    draft: Draft,
    target: Target,
    *,
    send_cover: bool | None = None,
    approved: bool = False,
) -> SendLog:
    """Send whatever this draft carries - text, poll, or generated file.

    Refuses while an approval is outstanding. This lives at the gate rather
    than in each caller on purpose: "Send now", the scheduler and the manual
    per-draft send each reached WhatsApp without consulting approvals, so a
    draft awaiting sign-off on the phone went to the community anyway. A guard
    here covers every path, including ones added later.
    """
    if not approved:
        waiting = pending_approval(session, draft)
        if waiting is not None:
            raise AwaitingApproval(
                f"This draft is waiting for approval on WhatsApp (code {waiting.code}). "
                f"Reply 'approve {waiting.code}' on your phone to send it, "
                f"or 'reject {waiting.code}' to discard it."
            )

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
