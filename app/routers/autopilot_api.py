"""Autopilot: standing briefs that decide their own content each run."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_session, session_scope
from ..models import (
    Autopilot,
    Campaign,
    CampaignTarget,
    Engine,
    OutputType,
    Schedule,
    Target,
)
from ..services import autopilot as runner
from ..services import composer, jobs, planner
from ..util import fmt_local, relative

router = APIRouter(prefix="/api/autopilots", tags=["autopilot"])

FORMATS = ["message", "pdf", "excel", "poll"]


class AutopilotIn(BaseModel):
    name: str = ""
    context: str
    avoid: str = ""
    target_ids: list[int] = []
    language: str = "hinglish"
    engine: Engine = Engine.openrouter
    model_override: str | None = None
    format_mode: Literal["auto", "message", "pdf", "excel", "poll"] = "auto"
    formats: list[str] = FORMATS
    use_research: bool = True
    send_cover_image: bool = True
    approval_required: bool = True
    # Autopilot fires unattended, so "whatsapp" is the useful setting.
    approval_mode: str = "dashboard"


class AutopilotPatch(BaseModel):
    name: str | None = None
    context: str | None = None
    avoid: str | None = None
    target_ids: list[int] | None = None
    language: str | None = None
    engine: Engine | None = None
    model_override: str | None = None
    format_mode: Literal["auto", "message", "pdf", "excel", "poll"] | None = None
    formats: list[str] | None = None
    use_research: bool | None = None
    send_cover_image: bool | None = None
    approval_required: bool | None = None
    approval_mode: str | None = None
    active: bool | None = None


def _get_or_404(session: Session, autopilot_id: int) -> Autopilot:
    pilot = session.get(Autopilot, autopilot_id)
    if pilot is None:
        raise HTTPException(status_code=404, detail="Autopilot not found.")
    return pilot


def _serialise(session: Session, pilot: Autopilot) -> dict[str, Any]:
    campaign = session.get(Campaign, pilot.campaign_id)
    targets = composer.campaign_targets(session, pilot.campaign_id) if campaign else []
    schedules = session.exec(
        select(Schedule).where(Schedule.campaign_id == pilot.campaign_id)
    ).all()

    from .schedules_api import _serialise as serialise_schedule

    return {
        "id": pilot.id,
        "name": pilot.name,
        "campaign_id": pilot.campaign_id,
        "context": pilot.context,
        "avoid": pilot.avoid,
        "format_mode": pilot.format_mode,
        "formats": pilot.formats,
        "approval_required": pilot.approval_required,
        "approval_mode": pilot.approval_mode,
        "active": pilot.active,
        "language": campaign.language if campaign else "hinglish",
        "engine": campaign.engine.value if campaign else "openrouter",
        "model_override": campaign.model_override if campaign else None,
        "use_research": campaign.use_research if campaign else True,
        "send_cover_image": campaign.send_cover_image if campaign else True,
        "target_ids": [t.id for t in targets],
        "targets": [{"id": t.id, "name": t.name, "type": t.type.value} for t in targets],
        "recent_topics": (pilot.recent_topics or [])[:10],
        "recent_formats": (pilot.recent_formats or [])[:6],
        "last_plan": pilot.last_plan,
        "last_run_at": pilot.last_run_at.isoformat() if pilot.last_run_at else None,
        "last_run_label": relative(pilot.last_run_at) if pilot.last_run_at else None,
        "schedules": [serialise_schedule(s) for s in schedules],
        "next_run_label": next(
            (serialise_schedule(s)["next_run_label"] for s in schedules
             if serialise_schedule(s)["next_run_label"]), None
        ),
    }


def _sync_targets(session: Session, campaign_id: int, target_ids: list[int]) -> None:
    for link in session.exec(
        select(CampaignTarget).where(CampaignTarget.campaign_id == campaign_id)
    ).all():
        session.delete(link)
    for target_id in dict.fromkeys(target_ids):
        if session.get(Target, target_id) is None:
            raise HTTPException(status_code=404, detail=f"Target {target_id} not found.")
        session.add(CampaignTarget(campaign_id=campaign_id, target_id=target_id))
    session.commit()


@router.get("")
def list_autopilots(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = session.exec(select(Autopilot).order_by(Autopilot.id.desc())).all()
    return [_serialise(session, p) for p in rows]


@router.post("", status_code=201)
def create_autopilot(
    payload: AutopilotIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if not payload.context.strip():
        raise HTTPException(
            status_code=400, detail="Describe the community so it knows what to think about."
        )
    if not payload.target_ids:
        raise HTTPException(status_code=400, detail="Pick at least one chat.")

    campaign = Campaign(
        name=payload.name.strip() or "Autopilot",
        brief=payload.context.strip(),   # replaced by the planner on every run
        language=payload.language,
        engine=payload.engine,
        output_type=OutputType.message,
        model_override=(payload.model_override or "").strip() or None,
        use_research=payload.use_research,
        send_cover_image=payload.send_cover_image,
    )
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    _sync_targets(session, campaign.id, payload.target_ids)

    pilot = Autopilot(
        name=payload.name.strip() or "Autopilot",
        campaign_id=campaign.id,
        context=payload.context.strip(),
        avoid=payload.avoid.strip(),
        format_mode=payload.format_mode,
        formats=[f for f in payload.formats if f in FORMATS] or FORMATS,
        approval_required=payload.approval_required,
        approval_mode=payload.approval_mode,
    )
    session.add(pilot)
    session.commit()
    session.refresh(pilot)
    return _serialise(session, pilot)


@router.get("/{autopilot_id}")
def read_autopilot(autopilot_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return _serialise(session, _get_or_404(session, autopilot_id))


@router.patch("/{autopilot_id}")
def update_autopilot(
    autopilot_id: int, payload: AutopilotPatch, session: Session = Depends(get_session)
) -> dict[str, Any]:
    pilot = _get_or_404(session, autopilot_id)
    campaign = session.get(Campaign, pilot.campaign_id)
    data = payload.model_dump(exclude_unset=True)

    target_ids = data.pop("target_ids", None)
    for key in ("language", "engine", "model_override", "use_research", "send_cover_image"):
        if key in data and campaign is not None:
            setattr(campaign, key, data.pop(key))
    if campaign is not None:
        session.add(campaign)

    if "formats" in data:
        data["formats"] = [f for f in (data["formats"] or []) if f in FORMATS] or FORMATS
    for key, value in data.items():
        setattr(pilot, key, value)

    session.add(pilot)
    session.commit()

    if target_ids is not None:
        _sync_targets(session, pilot.campaign_id, target_ids)

    session.refresh(pilot)
    return _serialise(session, pilot)


@router.delete("/{autopilot_id}", status_code=204)
def delete_autopilot(autopilot_id: int, session: Session = Depends(get_session)) -> None:
    from .campaigns_api import delete_campaign

    pilot = _get_or_404(session, autopilot_id)
    campaign_id = pilot.campaign_id
    session.delete(pilot)
    session.commit()
    # Removes the backing campaign's schedules and links too.
    delete_campaign(campaign_id, session)


@router.post("/{autopilot_id}/forget", status_code=200)
def forget_history(autopilot_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Clear the topic memory, so it may revisit subjects again."""
    pilot = _get_or_404(session, autopilot_id)
    pilot.recent_topics = []
    pilot.recent_formats = []
    session.add(pilot)
    session.commit()
    return _serialise(session, pilot)


@router.post("/{autopilot_id}/run")
async def run_now(
    autopilot_id: int,
    deliver: bool = False,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Think and write now. `deliver=false` stops at the preview."""
    pilot = _get_or_404(session, autopilot_id)
    job = jobs.create(kind="message", engine="openrouter")
    jobs.run(job, _run_in_background(autopilot_id, deliver, job))
    return {"job_id": job.id, **job.as_dict()}


async def _run_in_background(autopilot_id: int, deliver: bool, job: jobs.Job) -> dict[str, Any]:
    with session_scope() as session:
        pilot = session.get(Autopilot, autopilot_id)
        if pilot is None:
            raise RuntimeError("Autopilot was deleted while running.")
        campaign = session.get(Campaign, pilot.campaign_id)
        if campaign is not None:
            job.engine = campaign.engine.value
        try:
            return await runner.run(
                session, pilot, deliver=deliver, on_phase=job.set_phase
            )
        except runner.AutopilotError as exc:
            raise RuntimeError(str(exc)) from exc


@router.post("/{autopilot_id}/preview-plan")
async def preview_plan(
    autopilot_id: int, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Show what it would post next, without writing or sending anything."""
    pilot = _get_or_404(session, autopilot_id)
    campaign = runner.backing_campaign(session, pilot)
    try:
        decision = await planner.plan(session, pilot, campaign)
    except planner.PlannerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "topic": decision["topic"],
        "angle": decision["angle"],
        "format": decision["format"],
        "why": decision["why"],
        "repeat_warning": decision["repeat_warning"],
        "sources": decision["sources"],
        "model_used": decision["model_used"],
    }
