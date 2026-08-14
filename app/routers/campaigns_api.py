"""The Composer: brief -> generate -> preview -> send or schedule."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_session, session_scope
from ..models import (
    Campaign,
    CampaignTarget,
    Draft,
    DraftStatus,
    Engine,
    OutputType,
    Target,
)
from ..services import assets, composer, engines, jobs, sender, waha
from ..util import fmt_local, relative, utcnow

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


class CampaignIn(BaseModel):
    name: str = ""
    brief: str
    language: str = "hinglish"
    engine: Engine = Engine.openrouter
    output_type: OutputType = OutputType.message
    model_override: str | None = None
    extra_instructions: str = ""
    use_research: bool = True
    send_cover_image: bool = True
    approval_mode: Literal["per_target", "dashboard", "whatsapp"] = "per_target"
    generate_cover: bool = False
    target_ids: list[int] = []


class CampaignPatch(BaseModel):
    name: str | None = None
    brief: str | None = None
    language: str | None = None
    engine: Engine | None = None
    output_type: OutputType | None = None
    model_override: str | None = None
    extra_instructions: str | None = None
    use_research: bool | None = None
    send_cover_image: bool | None = None
    approval_mode: Literal["per_target", "dashboard", "whatsapp"] | None = None
    generate_cover: bool | None = None
    target_ids: list[int] | None = None


def _get_or_404(session: Session, campaign_id: int) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    return campaign


def _set_targets(session: Session, campaign_id: int, target_ids: list[int]) -> None:
    for link in session.exec(
        select(CampaignTarget).where(CampaignTarget.campaign_id == campaign_id)
    ).all():
        session.delete(link)

    for target_id in dict.fromkeys(target_ids):  # de-dupe, keep order
        if session.get(Target, target_id) is None:
            raise HTTPException(status_code=404, detail=f"Target {target_id} not found.")
        session.add(CampaignTarget(campaign_id=campaign_id, target_id=target_id))
    session.commit()


def _serialise(session: Session, campaign: Campaign) -> dict[str, Any]:
    targets = composer.campaign_targets(session, campaign.id)
    drafts = session.exec(
        select(Draft).where(Draft.campaign_id == campaign.id).order_by(Draft.id.desc())
    ).all()
    latest = drafts[0] if drafts else None
    return {
        "id": campaign.id,
        "name": campaign.name,
        "brief": campaign.brief,
        "language": campaign.language,
        "engine": campaign.engine.value,
        "output_type": campaign.output_type.value,
        "model_override": campaign.model_override,
        "extra_instructions": campaign.extra_instructions,
        "use_research": campaign.use_research,
        "send_cover_image": campaign.send_cover_image,
        "approval_mode": campaign.approval_mode,
        "generate_cover": campaign.generate_cover,
        "target_ids": [t.id for t in targets],
        "targets": [{"id": t.id, "name": t.name, "type": t.type.value} for t in targets],
        "created_at": campaign.created_at.isoformat(),
        "last_run_at": campaign.last_run_at.isoformat() if campaign.last_run_at else None,
        "last_run_label": relative(campaign.last_run_at) if campaign.last_run_at else None,
        "pending": sum(1 for d in drafts if d.status == DraftStatus.pending),
        "sent": sum(1 for d in drafts if d.status == DraftStatus.sent),
        "preview": {
            "content": latest.content,
            "poll_options": latest.poll_options,
            "asset_filename": latest.asset_filename,
            "output_type": latest.output_type.value,
            "model_used": latest.model_used,
            "status": latest.status.value,
        } if latest else None,
    }


@router.get("/engines")
async def engine_availability(session: Session = Depends(get_session)) -> dict[str, Any]:
    return await engines.availability(session)


@router.get("")
def list_campaigns(
    limit: int = 30, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    rows = session.exec(
        select(Campaign).order_by(Campaign.created_at.desc()).limit(limit)
    ).all()
    return [_serialise(session, c) for c in rows]


@router.post("", status_code=201)
def create_campaign(
    payload: CampaignIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if not payload.brief.strip():
        raise HTTPException(status_code=400, detail="Write a brief first.")

    campaign = Campaign(
        name=payload.name.strip(),
        brief=payload.brief.strip(),
        language=payload.language.strip() or "hinglish",
        engine=payload.engine,
        output_type=payload.output_type,
        model_override=(payload.model_override or "").strip() or None,
        extra_instructions=payload.extra_instructions.strip(),
        use_research=payload.use_research,
        send_cover_image=payload.send_cover_image,
        approval_mode=payload.approval_mode,
        generate_cover=payload.generate_cover,
    )
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    _set_targets(session, campaign.id, payload.target_ids)
    return _serialise(session, campaign)


@router.get("/{campaign_id}")
def read_campaign(campaign_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return _serialise(session, _get_or_404(session, campaign_id))


@router.patch("/{campaign_id}")
def update_campaign(
    campaign_id: int, payload: CampaignPatch, session: Session = Depends(get_session)
) -> dict[str, Any]:
    campaign = _get_or_404(session, campaign_id)
    data = payload.model_dump(exclude_unset=True)
    target_ids = data.pop("target_ids", None)

    for key, value in data.items():
        setattr(campaign, key, value)
    session.add(campaign)
    session.commit()

    if target_ids is not None:
        _set_targets(session, campaign_id, target_ids)

    session.refresh(campaign)
    return _serialise(session, campaign)


@router.delete("/{campaign_id}", status_code=204)
def delete_campaign(campaign_id: int, session: Session = Depends(get_session)) -> None:
    """Remove a campaign and everything that points at it.

    Explicit ordered statements: schedules and links reference the campaign, so
    they have to go first or the FK constraint refuses the delete. Drafts are
    kept - they carry the send history - and simply detached.
    """
    from sqlalchemy import delete, update

    from ..models import ApprovalRequest, Schedule

    _get_or_404(session, campaign_id)

    # Approvals reference the campaign, so they go before it - SQLAlchemy will
    # not order these for us, and the FK fails if the campaign goes first.
    session.exec(delete(ApprovalRequest).where(ApprovalRequest.campaign_id == campaign_id))
    session.exec(delete(Schedule).where(Schedule.campaign_id == campaign_id))
    session.exec(delete(CampaignTarget).where(CampaignTarget.campaign_id == campaign_id))
    session.exec(
        update(Draft).where(Draft.campaign_id == campaign_id).values(campaign_id=None)
    )
    session.exec(delete(Campaign).where(Campaign.id == campaign_id))
    session.commit()

    from ..services import scheduler

    scheduler.reload_jobs()


@router.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    """Progress for a running generation. Polled by the Composer."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="That job has expired.")
    return job.as_dict()


@router.post("/{campaign_id}/generate")
async def generate(
    campaign_id: int, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Kick off generation in the background and return a job to poll.

    Must be async: a sync endpoint runs in a threadpool with no event loop,
    so scheduling the background task would fail.

    A Claude Code PDF can take two minutes; holding the request open that long
    gives the browser nothing to display and risks a proxy timeout.
    """
    campaign = _get_or_404(session, campaign_id)

    # One generation at a time per campaign. Two concurrent runs both wrote
    # drafts for the same targets, and a single approval then released both.
    in_flight = jobs.running_for(campaign_id)
    if in_flight is not None:
        raise HTTPException(
            status_code=409,
            detail="This campaign is already generating. Wait for it to finish.",
        )

    # Any approval still outstanding was about the draft we are replacing, so
    # retire it - otherwise the old code in the chat stays a live trigger and
    # would release the NEW, unreviewed content.
    from ..services import approvals as _approvals

    _approvals.supersede(session, campaign_id)

    # Clear the previous preview so repeated generates do not pile up.
    for draft in session.exec(
        select(Draft).where(
            Draft.campaign_id == campaign_id, Draft.status == DraftStatus.pending
        )
    ).all():
        session.delete(draft)
    session.commit()

    job = jobs.create(kind=campaign.output_type.value, engine=campaign.engine.value,
                      campaign_id=campaign_id)
    jobs.run(job, _generate_in_background(campaign_id, job))
    return {"job_id": job.id, **job.as_dict()}


async def _generate_in_background(campaign_id: int, job: jobs.Job) -> dict[str, Any]:
    """Runs detached, so it needs its own DB session."""
    with session_scope() as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise RuntimeError("Campaign was deleted while generating.")
        try:
            result = await composer.run_campaign(
                session, campaign, on_phase=job.set_phase
            )
        except composer.ComposerError as exc:
            raise RuntimeError(str(exc)) from exc

        # Sign-off happens on WhatsApp: push the draft to your own number and
        # wait for the reply rather than for a click in the dashboard. Each
        # chat decides for itself, so one campaign can raise several requests -
        # or none.
        from ..services import approvals

        drafts = session.exec(
            select(Draft).where(
                Draft.campaign_id == campaign.id,
                Draft.status == DraftStatus.pending,
            )
        ).all()

        raised: list[dict[str, Any]] = []
        for draft in drafts:
            target = session.get(Target, draft.target_id)
            if target is None:
                continue
            if approvals.resolve_mode(campaign, target) != "whatsapp":
                continue
            job.set_phase("saving", f"Sending {target.name} for approval on WhatsApp…")
            try:
                info = await approvals.request(session, campaign, [draft], target)
            except Exception as exc:
                # Never lose the draft over a failed notification - it stays
                # pending and can still be approved in the dashboard.
                result.setdefault("approval_error", str(exc))
                continue
            raised.append({**info, "target": target.name, "target_id": target.id})

        if raised:
            result["approvals"] = raised
            # The composer banner reads a single `approval`; keep it populated
            # for the common one-chat case.
            result["approval"] = raised[0]

        return result


@router.post("/{campaign_id}/send")
async def send_now(
    campaign_id: int, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Send every pending draft for this campaign."""
    _get_or_404(session, campaign_id)
    drafts = session.exec(
        select(Draft).where(
            Draft.campaign_id == campaign_id, Draft.status == DraftStatus.pending
        )
    ).all()
    if not drafts:
        raise HTTPException(
            status_code=409, detail="Nothing to send — generate the content first."
        )

    sent, failed, waiting = 0, [], []
    for draft in drafts:
        target = session.get(Target, draft.target_id)
        if target is None:
            draft.status = DraftStatus.failed
            draft.error = "Target no longer exists."
            session.add(draft)
            failed.append(draft.error)
            continue
        try:
            await sender.send_draft(session, draft, target)
        except sender.AwaitingApproval as exc:
            # Not a failure - it is waiting on you. Leave it pending so the
            # WhatsApp reply can still release it.
            waiting.append(f"{target.name}: {exc}")
            continue
        except (sender.SendBlocked, waha.WahaError) as exc:
            message = getattr(exc, "message", str(exc))
            draft.status = DraftStatus.failed
            draft.error = message
            session.add(draft)
            failed.append(f"{target.name}: {message}")
            continue

        draft.status = DraftStatus.sent
        draft.sent_at = utcnow()
        draft.error = None
        session.add(draft)
        sent += 1

    session.commit()
    return {
        "ok": not failed,
        "sent": sent,
        "failed": len(failed),
        "errors": failed[:5],
        # Surfaced separately from errors: these drafts are intact and still
        # waiting on a WhatsApp reply, not broken.
        "awaiting_approval": len(waiting),
        "awaiting": waiting[:5],
    }


@router.get("/{campaign_id}/asset")
def download_asset(campaign_id: int, session: Session = Depends(get_session)) -> FileResponse:
    """Download the generated PDF/Excel without sending it."""
    _get_or_404(session, campaign_id)
    draft = session.exec(
        select(Draft)
        .where(Draft.campaign_id == campaign_id, Draft.asset_path.is_not(None))
        .order_by(Draft.id.desc())
    ).first()
    if draft is None or not draft.asset_path:
        raise HTTPException(status_code=404, detail="This campaign has no generated file.")

    path = Path(draft.asset_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="The file is no longer on disk.")
    return FileResponse(
        path,
        media_type=draft.asset_mime or "application/octet-stream",
        filename=draft.asset_filename or path.name,
    )


@router.get("/{campaign_id}/asset/inline")
def inline_asset(campaign_id: int, session: Session = Depends(get_session)) -> FileResponse:
    """Same file, served inline so a PDF can render in an iframe preview."""
    _get_or_404(session, campaign_id)
    draft = session.exec(
        select(Draft)
        .where(Draft.campaign_id == campaign_id, Draft.asset_path.is_not(None))
        .order_by(Draft.id.desc())
    ).first()
    if draft is None or not draft.asset_path:
        raise HTTPException(status_code=404, detail="This campaign has no generated file.")

    path = Path(draft.asset_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="The file is no longer on disk.")
    return FileResponse(
        path,
        media_type=draft.asset_mime or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


@router.get("/{campaign_id}/cover")
def cover_image(campaign_id: int, session: Session = Depends(get_session)) -> FileResponse:
    """The preview image a channel post leads with."""
    _get_or_404(session, campaign_id)
    draft = session.exec(
        select(Draft)
        .where(Draft.campaign_id == campaign_id, Draft.image_path.is_not(None))
        .order_by(Draft.id.desc())
    ).first()
    if draft is None or not draft.image_path:
        raise HTTPException(status_code=404, detail="No preview image for this campaign.")

    path = Path(draft.image_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="The image is no longer on disk.")
    return FileResponse(path, media_type=draft.image_mime or "image/png")


@router.get("/{campaign_id}/asset/preview")
def asset_preview(campaign_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Structured preview (sheet rows / PDF outline) for rendering in-page."""
    _get_or_404(session, campaign_id)
    draft = session.exec(
        select(Draft)
        .where(Draft.campaign_id == campaign_id, Draft.asset_path.is_not(None))
        .order_by(Draft.id.desc())
    ).first()
    if draft is None or not draft.asset_path:
        raise HTTPException(status_code=404, detail="This campaign has no generated file.")
    return assets.preview(draft.asset_path, draft.output_type.value)


@router.get("/{campaign_id}/schedules")
def campaign_schedules(
    campaign_id: int, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    from ..models import Schedule
    from .schedules_api import _serialise as serialise_schedule

    _get_or_404(session, campaign_id)
    rows = session.exec(
        select(Schedule).where(Schedule.campaign_id == campaign_id).order_by(Schedule.id)
    ).all()
    return [serialise_schedule(r) for r in rows]
