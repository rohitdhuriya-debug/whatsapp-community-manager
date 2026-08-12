"""Server-rendered HTML pages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from ..db import get_session
from ..models import Draft, DraftStatus, Target
from ..web import page

router = APIRouter(include_in_schema=False)


@router.get("/")
def dashboard(request: Request, session: Session = Depends(get_session)):
    targets = session.exec(select(Target).order_by(Target.name)).all()
    return page(
        request,
        "dashboard.html",
        session,
        active_page="dashboard",
        targets=targets,
    )


@router.get("/compose")
def compose_page(request: Request, session: Session = Depends(get_session)):
    return page(request, "composer.html", session, active_page="compose")


@router.get("/news")
def news_page(request: Request, session: Session = Depends(get_session)):
    return page(request, "news.html", session, active_page="news")


@router.get("/autopilot")
def autopilot_page(request: Request, session: Session = Depends(get_session)):
    return page(request, "autopilot.html", session, active_page="autopilot")


@router.get("/devices")
def devices_page(request: Request, session: Session = Depends(get_session)):
    return page(request, "devices.html", session, active_page="devices")


@router.get("/assets")
def assets_page(request: Request, session: Session = Depends(get_session)):
    return page(request, "assets.html", session, active_page="assets")


@router.get("/settings")
def settings_page(request: Request, session: Session = Depends(get_session)):
    return page(request, "settings.html", session, active_page="settings")


@router.get("/targets/new")
def new_target_page(request: Request, session: Session = Depends(get_session)):
    return page(request, "target_new.html", session, active_page="new_target")


@router.get("/targets/{target_id}")
def target_page(
    target_id: int, request: Request, session: Session = Depends(get_session)
):
    target = session.get(Target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    drafts = session.exec(
        select(Draft)
        .where(Draft.target_id == target_id)
        .order_by(Draft.created_at.desc())
        .limit(50)
    ).all()
    from ..services.pipeline import FINANCE_DISCLAIMER, FINANCE_PATTERN

    blob = f"{target.niche} {target.persona_prompt} {target.research_instructions}"
    return page(
        request,
        "target.html",
        session,
        active_page="target",
        active_target_id=target_id,
        target=target,
        disclaimer_text=FINANCE_DISCLAIMER,
        disclaimer_auto=FINANCE_PATTERN.search(blob) is not None,
        drafts=drafts,
        pending_drafts=[d for d in drafts if d.status == DraftStatus.pending],
    )
