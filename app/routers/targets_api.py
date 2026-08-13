"""Targets CRUD, dashboard summary, and the manual test send."""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlmodel import Session, select

from ..config import is_free_model
from ..db import get_session
from ..models import (
    ContentType,
    Draft,
    DraftStatus,
    Language,
    SendLog,
    Target,
    TargetType,
)
from ..services import sender, waha
from ..util import fmt_local, relative

router = APIRouter(prefix="/api/targets", tags=["targets"])

GROUP_SUFFIX = "@g.us"
CHANNEL_SUFFIX = "@newsletter"


TELEGRAM_ID = re.compile(r"^-?\d{5,}$")


def is_telegram_chat(chat_id: str) -> bool:
    """Telegram chat ids are bare numbers; WhatsApp ids always carry a suffix."""
    return bool(TELEGRAM_ID.fullmatch(chat_id.strip()))


def _validate_chat_id(chat_id: str, target_type: TargetType) -> str:
    chat_id = chat_id.strip()

    # Telegram: a numeric id, and any of group / supergroup / channel is valid.
    if is_telegram_chat(chat_id):
        return chat_id

    if target_type == TargetType.supergroup:
        raise ValueError(
            f"'supergroup' is a Telegram type, but '{chat_id}' is not a Telegram chat id."
        )

    expected = GROUP_SUFFIX if target_type == TargetType.group else CHANNEL_SUFFIX
    if not chat_id.endswith(expected):
        raise ValueError(
            f"A '{target_type.value}' chat_id must end with '{expected}' (got '{chat_id}')."
        )
    return chat_id


class TargetIn(BaseModel):
    name: str
    niche: str = ""
    type: TargetType = TargetType.group
    chat_id: str
    device_id: int | None = None
    persona_prompt: str = ""
    research_instructions: str = ""
    tone: str = ""
    language: Language = Language.hinglish
    example_messages: list[str] = []
    banned_topics: str = ""
    cta_link: str = ""
    model_override: str | None = None
    disclaimer_mode: Literal["auto", "always", "never"] = "auto"
    approval_required: bool = True
    # "dashboard" | "whatsapp" - where this chat's drafts get signed off.
    approval_mode: str = "dashboard"
    enabled: bool = True

    @field_validator("model_override")
    @classmethod
    def _free_only(cls, v: str | None) -> str | None:
        v = (v or "").strip() or None
        if v and not is_free_model(v):
            raise ValueError(f"model_override '{v}' must end with ':free'.")
        return v


class TargetPatch(BaseModel):
    name: str | None = None
    niche: str | None = None
    type: TargetType | None = None
    chat_id: str | None = None
    device_id: int | None = None
    persona_prompt: str | None = None
    research_instructions: str | None = None
    tone: str | None = None
    language: Language | None = None
    example_messages: list[str] | None = None
    banned_topics: str | None = None
    cta_link: str | None = None
    model_override: str | None = None
    disclaimer_mode: Literal["auto", "always", "never"] | None = None
    approval_required: bool | None = None
    approval_mode: str | None = None
    enabled: bool | None = None

    @field_validator("model_override")
    @classmethod
    def _free_only(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not is_free_model(v):
            raise ValueError(f"model_override '{v}' must end with ':free'.")
        return v


class TestSend(BaseModel):
    text: str


class ComposeNow(BaseModel):
    content_type: ContentType = ContentType.news


def _get_or_404(session: Session, target_id: int) -> Target:
    target = session.get(Target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found.")
    return target


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get("")
def list_targets(session: Session = Depends(get_session)) -> list[Target]:
    return session.exec(select(Target).order_by(Target.name)).all()


@router.get("/summary")
def summary(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Powers the dashboard cards: next scheduled run and last successful send."""
    from ..services import scheduler  # imported lazily; registered in Phase 4

    rows: list[dict[str, Any]] = []
    for target in session.exec(select(Target).order_by(Target.name)).all():
        last = session.exec(
            select(SendLog)
            .where(SendLog.target_id == target.id, SendLog.status == "sent")
            .order_by(SendLog.created_at.desc())
        ).first()
        next_run = scheduler.next_run_for_target(target.id)
        rows.append(
            {
                "id": target.id,
                "name": target.name,
                "enabled": target.enabled,
                "next_run": next_run.isoformat() if next_run else None,
                "next_run_label": fmt_local(next_run) if next_run else None,
                "last_sent": last.created_at.isoformat() if last else None,
                "last_sent_label": relative(last.created_at) if last else None,
            }
        )
    return rows


@router.get("/{target_id}")
def read_target(target_id: int, session: Session = Depends(get_session)) -> Target:
    return _get_or_404(session, target_id)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


class ChatRef(BaseModel):
    chat_id: str
    name: str = ""
    type: TargetType | None = None
    device_id: int | None = None
    # Set when ticking chats in the Composer, so a channel can be created
    # already requiring a phone sign-off.
    approval_mode: str | None = None


class EnsureTargets(BaseModel):
    chats: list[ChatRef]


@router.post("/ensure")
def ensure_targets(
    payload: EnsureTargets, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    """Register picked chats as targets so they become sendable (HC-7).

    The Composer lets you tick chats straight off WhatsApp; this is what turns
    a tick into a saved target. Existing ones are returned untouched, so a
    persona you configured earlier is never overwritten.
    """
    out: list[dict[str, Any]] = []
    for chat in payload.chats:
        chat_id = chat.chat_id.strip()
        if not chat_id:
            continue

        existing = session.exec(select(Target).where(Target.chat_id == chat_id)).first()
        if existing:
            out.append({"id": existing.id, "name": existing.name,
                        "chat_id": existing.chat_id,
                        "approval_mode": existing.approval_mode, "created": False})
            continue

        if chat.type is not None:
            kind = chat.type
        elif chat_id.endswith(CHANNEL_SUFFIX):
            kind = TargetType.channel
        else:
            kind = TargetType.group
        try:
            validated = _validate_chat_id(chat_id, kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        mode = (chat.approval_mode or "").strip()
        target = Target(
            name=chat.name.strip() or validated,
            type=kind,
            chat_id=validated,
            device_id=chat.device_id,
            approval_mode=mode if mode in ("dashboard", "whatsapp") else "dashboard",
        )
        session.add(target)
        session.commit()
        session.refresh(target)
        out.append({"id": target.id, "name": target.name,
                    "chat_id": target.chat_id,
                    "approval_mode": target.approval_mode, "created": True})
    return out


@router.post("", status_code=201)
def create_target(payload: TargetIn, session: Session = Depends(get_session)) -> Target:
    try:
        chat_id = _validate_chat_id(payload.chat_id, payload.type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if session.exec(select(Target).where(Target.chat_id == chat_id)).first():
        raise HTTPException(
            status_code=409, detail=f"A target with chat_id {chat_id} already exists."
        )

    target = Target(**{**payload.model_dump(), "chat_id": chat_id})
    session.add(target)
    session.commit()
    session.refresh(target)
    return target


@router.patch("/{target_id}")
def update_target(
    target_id: int, payload: TargetPatch, session: Session = Depends(get_session)
) -> Target:
    target = _get_or_404(session, target_id)
    data = payload.model_dump(exclude_unset=True)

    if "chat_id" in data or "type" in data:
        new_type = data.get("type", target.type)
        new_chat = data.get("chat_id", target.chat_id)
        try:
            data["chat_id"] = _validate_chat_id(new_chat, new_type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        clash = session.exec(
            select(Target).where(
                Target.chat_id == data["chat_id"], Target.id != target_id
            )
        ).first()
        if clash:
            raise HTTPException(
                status_code=409, detail=f"{data['chat_id']} is already used by '{clash.name}'."
            )

    for key, value in data.items():
        setattr(target, key, value)
    session.add(target)
    session.commit()
    session.refresh(target)
    return target


@router.delete("/{target_id}", status_code=204)
def delete_target(target_id: int, session: Session = Depends(get_session)) -> None:
    target = _get_or_404(session, target_id)

    from sqlalchemy import delete

    from ..models import CampaignTarget, Schedule

    # Explicit, ordered DELETE statements rather than session.delete().
    #
    # send_log rows reference drafts, so they must go first. The ORM's flush
    # does not reliably emit them in that order here (it batched the drafts
    # delete first and tripped the FK constraint), and statement order is not
    # something to leave to chance during a destructive cascade.
    # select(Draft.id) yields plain ints, not Draft rows.
    draft_ids = list(
        session.exec(select(Draft.id).where(Draft.target_id == target_id)).all()
    )

    session.exec(delete(SendLog).where(SendLog.target_id == target_id))
    if draft_ids:
        # A log row can reference this target's draft while belonging to
        # another target - catch those too.
        session.exec(delete(SendLog).where(SendLog.draft_id.in_(draft_ids)))
    session.exec(delete(Draft).where(Draft.target_id == target_id))
    session.exec(delete(CampaignTarget).where(CampaignTarget.target_id == target_id))
    session.exec(delete(Schedule).where(Schedule.target_id == target_id))
    # Approvals now carry a target_id, so they must go before the target.
    from ..models import ApprovalRequest

    session.exec(delete(ApprovalRequest).where(ApprovalRequest.target_id == target_id))
    session.exec(delete(Target).where(Target.id == target_id))
    session.commit()

    from ..services import scheduler

    scheduler.reload_jobs()


# ---------------------------------------------------------------------------
# Manual test send (Phase 2 - no AI involved)
# ---------------------------------------------------------------------------


@router.post("/{target_id}/send-test")
async def send_test(
    target_id: int, payload: TestSend, session: Session = Depends(get_session)
) -> dict[str, Any]:
    target = _get_or_404(session, target_id)
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message text is empty.")

    try:
        entry = await sender.send_to_target(session, target, text=text)
    except sender.SendBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except waha.WahaError as exc:
        raise HTTPException(
            status_code=502, detail=f"{exc.message} {exc.hint}".strip()
        ) from exc

    return {"ok": True, "log_id": entry.id, "chat_id": entry.chat_id}


@router.post("/{target_id}/compose-now")
async def compose_now(
    target_id: int, payload: ComposeNow, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Run research -> draft immediately. Respects the target's approval flag."""
    from ..services import pipeline

    _get_or_404(session, target_id)
    try:
        return await pipeline.run_for_target(
            target_id, payload.content_type.value, jitter=False
        )
    except pipeline.PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{target_id}/drafts")
def target_drafts(
    target_id: int,
    status: DraftStatus | None = None,
    session: Session = Depends(get_session),
) -> list[Draft]:
    _get_or_404(session, target_id)
    query = select(Draft).where(Draft.target_id == target_id)
    if status is not None:
        query = query.where(Draft.status == status)
    return session.exec(query.order_by(Draft.created_at.desc()).limit(100)).all()
