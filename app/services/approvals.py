"""Approve a draft by replying on WhatsApp.

The draft is sent to your own number with a short code. Replying
`/approve A7K2` there releases it to the communities; `/reject A7K2` discards
it. Sign-off from the phone, without opening the dashboard.

Replies are found by polling that one chat rather than by webhook: WAHA runs
in Docker, so a webhook would need container-to-host networking and a change
to the live session. Polling touches neither, and only runs while something is
actually waiting.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import string
from typing import Any

from sqlmodel import Session, select

from ..db import get_setting, session_scope, set_setting
from ..models import (
    ApprovalRequest,
    ApprovalStatus,
    Campaign,
    Device,
    Draft,
    DraftStatus,
    Platform,
    Target,
)
from ..util import utcnow
from . import composer, sender, waha

log = logging.getLogger(__name__)

CHAT_SETTING = "approval_whatsapp_chat"
POLL_SECONDS = 12
EXPIRE_MINUTES = 720          # 12h - a stale approval should not fire next week
FETCH_LIMIT = 15

APPROVE = re.compile(r"^\s*/?\s*(approve|ok|yes|send|haan|ha)\b\s*([a-z0-9]{4})?", re.I)
REJECT = re.compile(r"^\s*/?\s*(reject|no|cancel|stop|nahi)\b\s*([a-z0-9]{4})?", re.I)

_poller: asyncio.Task | None = None


def _fingerprint(text: str) -> str:
    """Short hash of the text a request showed, so release can verify it."""
    import hashlib

    return hashlib.sha1((text or "").encode("utf-8", "ignore")).hexdigest()[:16]


def _new_code() -> str:
    # No 0/O/1/I - these get typed back by hand on a phone.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alphabet) for _ in range(4))


# ---------------------------------------------------------------------------
# Where approvals go
# ---------------------------------------------------------------------------


def approval_chat(session: Session) -> tuple[str, str] | None:
    """(chat_id, session_name) for the approval chat, or None if unset.

    Defaults to the primary WhatsApp device's own number - the "Message
    yourself" chat - which needs no setup and always exists.
    """
    configured = get_setting(session, CHAT_SETTING, "").strip()

    device = session.exec(
        select(Device).where(Device.platform == Platform.whatsapp).order_by(
            Device.is_primary.desc(), Device.id
        )
    ).first()
    if device is None:
        return None

    if configured:
        chat = configured if "@" in configured else f"{configured.lstrip('+')}@c.us"
        return chat, device.session_name

    if not device.phone:
        return None
    return f"{device.phone}@c.us", device.session_name


def set_approval_chat(session: Session, value: str) -> None:
    set_setting(session, CHAT_SETTING, value.strip())


def resolve_mode(campaign: Campaign | None, target: Target) -> str:
    """Where THIS chat's draft gets signed off.

    The campaign can force one mode for everything; "per_target" (the default)
    hands the decision to the chat itself, so a channel can need a phone
    sign-off while a test group does not.
    """
    forced = (getattr(campaign, "approval_mode", "") or "per_target").strip()
    if forced in ("dashboard", "whatsapp"):
        return forced
    mode = (getattr(target, "approval_mode", "") or "dashboard").strip()
    return mode if mode in ("dashboard", "whatsapp") else "dashboard"


# ---------------------------------------------------------------------------
# Raising a request
# ---------------------------------------------------------------------------


def _summary(
    session: Session, campaign: Campaign, drafts: list[Draft],
    targets: list[Target] | None = None,
) -> str:
    # Must be the targets this request actually covers - naming every target in
    # the campaign would promise sends that this approval does not control.
    if targets is None:
        targets = composer.campaign_targets(session, campaign.id)
    names = ", ".join(t.name for t in targets[:4])
    if len(targets) > 4:
        names += f" +{len(targets) - 4} more"

    draft = drafts[0] if drafts else None
    lines = [
        f"*Approval needed*  #{campaign.id}",
        "",
        f"*To:* {names}  ({len(targets)} chat{'' if len(targets) == 1 else 's'})",
        f"*Type:* {campaign.output_type.value}",
    ]
    if draft and draft.asset_filename:
        lines.append(f"*File:* {draft.asset_filename}")
    return "\n".join(lines)


async def request(
    session: Session, campaign: Campaign, drafts: list[Draft],
    target: Target | None = None,
) -> dict[str, Any]:
    """Send the draft to your number and wait for a reply.

    Scoped to one target when given, so a campaign spanning chats with
    different approval modes releases only what was actually approved.
    """
    destination = approval_chat(session)
    if destination is None:
        raise RuntimeError(
            "No WhatsApp number to send approvals to. Link a WhatsApp device, or set "
            "an approval number in Settings."
        )
    chat_id, session_name = destination
    code = _new_code()
    draft = drafts[0] if drafts else None

    body = (
        f"{_summary(session, campaign, drafts, [target] if target else None)}\n"
        f"{'─' * 18}\n\n"
        f"{(draft.content if draft else '')}\n\n"
        f"{'─' * 18}\n"
        f"Reply *approve {code}* to send it.\n"
        f"Reply *reject {code}* to discard."
    )

    result = await waha.send_text(chat_id, body, session_name=session_name)

    # Only replies newer than this count, so an older "ok" in the chat cannot
    # release a draft it was never about.
    sent_ts = 0
    if isinstance(result, dict):
        raw = result.get("timestamp") or (result.get("_data") or {}).get("t")
        if isinstance(raw, (int, float)):
            sent_ts = int(raw)
    if not sent_ts:
        import time

        sent_ts = int(time.time())

    row = ApprovalRequest(
        campaign_id=campaign.id, code=code, chat_id=chat_id,
        session_name=session_name, sent_ts=sent_ts,
        target_id=target.id if target else None,
        # Pin the exact drafts this message showed, and their text.
        draft_ids=[d.id for d in drafts if d.id is not None],
        content_hash=_fingerprint(draft.content if draft else ""),
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    ensure_poller()
    log.info("Approval %s requested for campaign %s in %s", code, campaign.id, chat_id)
    return {"code": code, "chat_id": chat_id, "approval_id": row.id}


# ---------------------------------------------------------------------------
# Watching for the reply
# ---------------------------------------------------------------------------


def _match(body: str, code: str, *, require_code: bool = True) -> str | None:
    """'approved' / 'rejected' / None for one message body.

    The code is ALWAYS required. Approvals land in your own "message yourself"
    chat, which is also where people jot notes - a bare "ok" typed to yourself
    would broadcast to a community. Four characters is a small price for that
    not being possible.
    """
    text = (body or "").strip()
    if not text:
        return None

    for pattern, verdict in ((APPROVE, "approved"), (REJECT, "rejected")):
        found = pattern.match(text)
        if not found:
            continue
        typed = (found.group(2) or "").upper()
        if not typed:
            if require_code:
                continue
            return verdict
        if typed != code.upper():
            continue
        return verdict
    return None


async def _check_one(
    session: Session, row: ApprovalRequest, *, outstanding: int = 1
) -> bool:
    """Look for a verdict. Returns True if resolved."""
    try:
        messages = await waha.fetch_messages(
            row.chat_id, limit=FETCH_LIMIT, session_name=row.session_name
        )
    except waha.WahaError as exc:
        log.warning("Approval %s: could not read chat: %s", row.code, exc.message)
        return False

    for message in messages:
        if not isinstance(message, dict):
            continue
        timestamp = message.get("timestamp") or 0
        if not isinstance(timestamp, (int, float)) or int(timestamp) <= row.sent_ts:
            continue
        verdict = _match(str(message.get("body") or ""), row.code)
        if verdict is None:
            continue

        log.info("Approval %s -> %s", row.code, verdict)
        if verdict == "approved":
            await _release(session, row)
        else:
            await _discard(session, row)
        return True

    # Do not let an unanswered request hang around for ever.
    age = (utcnow() - row.created_at).total_seconds() / 60
    if age > EXPIRE_MINUTES:
        row.status = ApprovalStatus.expired
        row.resolved_at = utcnow()
        session.add(row)
        session.commit()
        log.info("Approval %s expired after %.0f min", row.code, age)
        return True
    return False


def _covered(session: Session, row: ApprovalRequest) -> list[Draft]:
    """The exact drafts this approval showed, if they are still pending.

    Pinned by id rather than re-queried. Re-querying "what is pending for this
    campaign now" meant that regenerating between the request and the reply
    silently swapped the content: you approved the text on your phone and a
    different, unreviewed draft went out.

    Rows created before draft_ids existed fall back to the old campaign+target
    query, which is what they were released by at the time.
    """
    if row.draft_ids:
        found = []
        for draft_id in row.draft_ids:
            draft = session.get(Draft, draft_id)
            if draft is None or draft.status != DraftStatus.pending:
                continue
            # SQLite reuses rowids, so a deleted-and-recreated draft can land on
            # the same id. Verify the text is the text that was shown.
            if row.content_hash and _fingerprint(draft.content) != row.content_hash:
                log.warning(
                    "Approval %s: draft %s no longer holds the approved text; skipping.",
                    row.code, draft_id,
                )
                continue
            found.append(draft)
        return found

    stmt = select(Draft).where(
        Draft.campaign_id == row.campaign_id, Draft.status == DraftStatus.pending
    )
    if row.target_id is not None:
        stmt = stmt.where(Draft.target_id == row.target_id)
    return list(session.exec(stmt).all())


def supersede(session: Session, campaign_id: int) -> int:
    """Expire outstanding approvals for a campaign whose drafts are being replaced.

    Called wherever pending drafts are deleted. Without it the old code stays a
    live trigger in the chat for content that no longer exists.
    """
    rows = session.exec(
        select(ApprovalRequest).where(
            ApprovalRequest.campaign_id == campaign_id,
            ApprovalRequest.status == ApprovalStatus.pending,
        )
    ).all()
    for row in rows:
        row.status = ApprovalStatus.expired
        row.resolved_at = utcnow()
        row.note = "superseded by a newer draft"
        session.add(row)
    if rows:
        session.commit()
        log.info("Superseded %s outstanding approval(s) for campaign %s",
                 len(rows), campaign_id)
    return len(rows)


async def _release(session: Session, row: ApprovalRequest) -> None:
    """Approved: send to the communities, then report back in the chat."""
    drafts = _covered(session, row)

    sent, failed = 0, []
    for draft in drafts:
        target = session.get(Target, draft.target_id)
        if target is None:
            continue
        try:
            # Released by the reply we just read - this is the approval.
            await sender.send_draft(session, draft, target, approved=True)
        except (sender.SendBlocked, waha.WahaError) as exc:
            message = getattr(exc, "message", str(exc))
            draft.status = DraftStatus.failed
            draft.error = message
            session.add(draft)
            failed.append(f"{target.name}: {message}")
            continue
        draft.status = DraftStatus.sent
        draft.sent_at = utcnow()
        session.add(draft)
        sent += 1

    row.status = ApprovalStatus.approved
    row.resolved_at = utcnow()
    row.note = f"sent {sent}, failed {len(failed)}"
    session.add(row)
    session.commit()

    reply = f"✅ Sent to {sent} chat{'' if sent == 1 else 's'}."
    if failed:
        reply += "\n\n⚠️ Failed:\n" + "\n".join(f"• {f}" for f in failed[:4])
    try:
        await waha.send_text(row.chat_id, reply, session_name=row.session_name)
    except waha.WahaError:
        pass  # the send itself already happened; the receipt is a nicety


async def _discard(session: Session, row: ApprovalRequest) -> None:
    """Rejected: mark the covered drafts and say so.

    Async, matching _release: the receipt used to be fired off with
    create_task, which needs a running loop and swallowed any error it hit.
    """
    for draft in _covered(session, row):
        draft.status = DraftStatus.rejected
        session.add(draft)

    row.status = ApprovalStatus.rejected
    row.resolved_at = utcnow()
    session.add(row)
    session.commit()

    await _say(row, "🗑️ Discarded. Nothing was sent.")


async def _say(row: ApprovalRequest, text: str) -> None:
    try:
        await waha.send_text(row.chat_id, text, session_name=row.session_name)
    except waha.WahaError:
        pass


def pending_count(session: Session) -> int:
    return len(
        session.exec(
            select(ApprovalRequest).where(ApprovalRequest.status == ApprovalStatus.pending)
        ).all()
    )


async def _loop() -> None:
    """Poll only while something is waiting, then stand down."""
    global _poller
    try:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            with session_scope() as session:
                rows = session.exec(
                    select(ApprovalRequest).where(
                        ApprovalRequest.status == ApprovalStatus.pending
                    )
                ).all()
                if not rows:
                    log.info("No approvals outstanding; poller standing down.")
                    return
                for row in rows:
                    try:
                        await _check_one(session, row, outstanding=len(rows))
                    except Exception:
                        log.exception("Approval %s check failed", row.code)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Approval poller died")
    finally:
        _poller = None


def ensure_poller() -> None:
    global _poller
    if _poller is not None and not _poller.done():
        return
    _poller = asyncio.create_task(_loop())
    log.info("Approval poller started.")
