"""Draft queue: list, edit, approve (which sends), reject."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_session
from ..models import Draft, DraftStatus, Target
from ..services import sender, waha
from ..util import utcnow

router = APIRouter(prefix="/api/drafts", tags=["drafts"])


class DraftPatch(BaseModel):
    content: str | None = None
    poll_options: list[str] | None = None


def _get_or_404(session: Session, draft_id: int) -> Draft:
    draft = session.get(Draft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found.")
    return draft


@router.get("")
def list_drafts(
    status: DraftStatus | None = None,
    target_id: int | None = None,
    limit: int = 100,
    session: Session = Depends(get_session),
) -> list[Draft]:
    query = select(Draft)
    if status is not None:
        query = query.where(Draft.status == status)
    if target_id is not None:
        query = query.where(Draft.target_id == target_id)
    return session.exec(query.order_by(Draft.created_at.desc()).limit(limit)).all()


@router.patch("/{draft_id}")
def edit_draft(
    draft_id: int, payload: DraftPatch, session: Session = Depends(get_session)
) -> Draft:
    """Edit the text before approving. Only pending drafts are editable."""
    draft = _get_or_404(session, draft_id)
    if draft.status != DraftStatus.pending:
        raise HTTPException(
            status_code=409,
            detail=f"Draft is '{draft.status.value}' — only pending drafts can be edited.",
        )

    data = payload.model_dump(exclude_unset=True)
    if "content" in data:
        content = (data["content"] or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="Message text cannot be empty.")
        draft.content = content
    if "poll_options" in data:
        options = [o.strip() for o in (data["poll_options"] or []) if o.strip()]
        draft.poll_options = options or None

    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft


@router.post("/{draft_id}/approve")
async def approve_draft(
    draft_id: int, session: Session = Depends(get_session)
) -> dict[str, Any]:
    draft = _get_or_404(session, draft_id)
    if draft.status not in (DraftStatus.pending, DraftStatus.failed):
        raise HTTPException(
            status_code=409,
            detail=f"Draft is already '{draft.status.value}'.",
        )

    target = session.get(Target, draft.target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target no longer exists.")

    try:
        await sender.send_draft(session, draft, target)
    except sender.SendBlocked as exc:
        draft.status = DraftStatus.failed
        draft.error = str(exc)
        session.add(draft)
        session.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except waha.WahaError as exc:
        draft.status = DraftStatus.failed
        draft.error = f"{exc.message} {exc.hint}".strip()
        session.add(draft)
        session.commit()
        raise HTTPException(status_code=502, detail=draft.error) from exc

    draft.status = DraftStatus.sent
    draft.sent_at = utcnow()
    draft.error = None
    session.add(draft)
    session.commit()
    return {"ok": True, "status": draft.status.value, "sent_at": draft.sent_at.isoformat()}


@router.post("/{draft_id}/reject")
def reject_draft(draft_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    draft = _get_or_404(session, draft_id)
    if draft.status == DraftStatus.sent:
        raise HTTPException(status_code=409, detail="Already sent — cannot reject.")
    draft.status = DraftStatus.rejected
    session.add(draft)
    session.commit()
    return {"ok": True, "status": draft.status.value}
