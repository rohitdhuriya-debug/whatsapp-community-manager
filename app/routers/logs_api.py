"""Send log (HC-7: every outbound attempt is recorded)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from ..db import get_session
from ..models import SendLog, Target
from ..util import fmt_local, relative

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
def read_logs(
    target_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(SendLog)
    if target_id is not None:
        query = query.where(SendLog.target_id == target_id)
    if status is not None:
        query = query.where(SendLog.status == status)

    rows = session.exec(query.order_by(SendLog.created_at.desc()).limit(limit)).all()
    names = {t.id: t.name for t in session.exec(select(Target)).all()}

    out: list[dict[str, Any]] = []
    for row in rows:
        payload = row.response_json or {}
        out.append(
            {
                "id": row.id,
                "target_id": row.target_id,
                "target_name": names.get(row.target_id, "deleted target"),
                "draft_id": row.draft_id,
                "chat_id": row.chat_id,
                "status": row.status,
                "error": payload.get("error"),
                "hint": payload.get("hint"),
                "created_at": row.created_at.isoformat(),
                "created_label": fmt_local(row.created_at),
                "created_relative": relative(row.created_at),
            }
        )
    return out
