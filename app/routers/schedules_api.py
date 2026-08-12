"""Schedule CRUD for both per-target personas and Composer campaigns.

Two kinds:
  * cron - recurring, 5-field expression, evaluated in Asia/Kolkata (HC-5)
  * once - a single date and time

Every write hot-reloads the scheduler's jobs, so nothing needs a restart.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from cron_descriptor import Options, get_description
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..config import config
from ..db import get_session
from ..models import Campaign, ContentType, Schedule, ScheduleKind, Target
from ..services import scheduler
from ..util import fmt_local, to_local

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


def describe(cron_expr: str) -> str:
    """'0 9 * * *' -> 'At 09:00 AM'. Falls back to the raw expression."""
    try:
        # locale_code is set explicitly; without it cron_descriptor warns on
        # machines that have no LANG/LC_ALL exported.
        options = Options()
        options.use_24hour_time_format = False
        options.locale_code = "en_US"
        return get_description(cron_expr, options)
    except Exception:
        return cron_expr


class ScheduleIn(BaseModel):
    target_id: int | None = None
    campaign_id: int | None = None
    kind: ScheduleKind = ScheduleKind.cron
    cron_expr: str = "0 9 * * *"
    run_at: str | None = None  # local IST, "YYYY-MM-DDTHH:MM"
    interval_days: int | None = None
    content_type: ContentType = ContentType.news
    active: bool = True


class PlanIn(BaseModel):
    """What the Composer sends: a date, a time, and how often to repeat.

    Cron is an implementation detail the UI should never have to think about.
    """

    campaign_id: int | None = None
    target_id: int | None = None
    repeat: Literal["once", "daily", "weekly", "alternate", "weekdays", "custom"] = "once"
    date: str | None = None       # "YYYY-MM-DD", for once / as the start day
    time: str = "09:00"           # "HH:MM" in Asia/Kolkata
    weekday: int = 0              # 0=Mon … 6=Sun, for weekly
    interval_days: int = 2        # for alternate / custom
    cron_expr: str | None = None  # escape hatch
    content_type: ContentType = ContentType.news


class SchedulePatch(BaseModel):
    cron_expr: str | None = None
    run_at: str | None = None
    content_type: ContentType | None = None
    active: bool | None = None


def _parse_local(value: str) -> datetime:
    """Accept a browser datetime-local string as IST, store as naive UTC."""
    text = (value or "").strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            naive = datetime.strptime(text[: len(fmt) + 2], fmt)
        except ValueError:
            continue
        aware = naive.replace(tzinfo=config.tz)
        if aware <= datetime.now(config.tz):
            raise HTTPException(
                status_code=400,
                detail=f"{fmt_local(aware.astimezone(timezone.utc).replace(tzinfo=None))} "
                "is in the past. Pick a future time.",
            )
        return aware.astimezone(timezone.utc).replace(tzinfo=None)
    raise HTTPException(
        status_code=400, detail=f"Could not read '{value}' as a date and time."
    )


def _serialise(row: Schedule) -> dict[str, Any]:
    next_run = None
    sched = scheduler.get_scheduler()
    if sched is not None:
        job = sched.get_job(f"schedule-{row.id}")
        next_run = job.next_run_time if job else None

    if row.kind == ScheduleKind.once:
        human = f"Once on {fmt_local(row.run_at)}"
    elif row.kind == ScheduleKind.interval:
        days = row.interval_days or 2
        every = "day" if days == 1 else ("other day" if days == 2 else f"{days} days")
        clock = to_local(row.run_at).strftime("%I:%M %p").lstrip("0") if row.run_at else ""
        human = f"Every {every}" + (f" at {clock}" if clock else "")
    else:
        human = describe(row.cron_expr)

    return {
        "id": row.id,
        "target_id": row.target_id,
        "campaign_id": row.campaign_id,
        "kind": row.kind.value,
        "cron_expr": row.cron_expr,
        "run_at": row.run_at.isoformat() if row.run_at else None,
        "run_at_label": fmt_local(row.run_at) if row.run_at else None,
        "content_type": row.content_type.value,
        "active": row.active,
        "human": human,
        "next_run": next_run.isoformat() if next_run else None,
        "next_run_label": fmt_local(next_run) if next_run else None,
    }


def _validate_cron(expr: str) -> str:
    expr = " ".join(expr.split())
    try:
        scheduler.parse_cron(expr)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid cron: {exc}") from exc
    return expr


@router.get("")
def list_schedules(
    target_id: int | None = None,
    campaign_id: int | None = None,
    active_only: bool = False,
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(Schedule)
    if target_id is not None:
        query = query.where(Schedule.target_id == target_id)
    if campaign_id is not None:
        query = query.where(Schedule.campaign_id == campaign_id)
    if active_only:
        query = query.where(Schedule.active)
    return [_serialise(r) for r in session.exec(query.order_by(Schedule.id)).all()]


@router.get("/upcoming")
def upcoming(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Everything scheduled, soonest first - powers the Scheduled tab."""
    rows = session.exec(select(Schedule).where(Schedule.active)).all()
    campaigns = {c.id: c for c in session.exec(select(Campaign)).all()}
    targets = {t.id: t for t in session.exec(select(Target)).all()}

    out = []
    for row in rows:
        item = _serialise(row)
        if row.campaign_id is not None:
            campaign = campaigns.get(row.campaign_id)
            item["label"] = (campaign.name or campaign.brief[:60]) if campaign else "—"
            item["subtitle"] = f"Campaign · {campaign.output_type.value}" if campaign else ""
        else:
            target = targets.get(row.target_id)
            item["label"] = target.name if target else "—"
            item["subtitle"] = f"Persona · {row.content_type.value}"
        out.append(item)

    out.sort(key=lambda i: i["next_run"] or "9999")
    return out


@router.get("/recent-times")
def recent_times(session: Session = Depends(get_session)) -> list[dict[str, str]]:
    """Times you have scheduled before, offered as one-tap presets."""
    rows = session.exec(select(Schedule).order_by(Schedule.id.desc()).limit(60)).all()

    seen: list[dict[str, str]] = []
    keys: set[str] = set()
    for row in rows:
        if row.kind == ScheduleKind.once and row.run_at:
            local = to_local(row.run_at)
            key = local.strftime("%H:%M")
            label = local.strftime("%I:%M %p").lstrip("0")
        else:
            key = row.cron_expr
            label = describe(row.cron_expr)
        if key in keys:
            continue
        keys.add(key)
        seen.append({"key": key, "label": label, "kind": row.kind.value,
                     "cron_expr": row.cron_expr})
        if len(seen) >= 6:
            break
    return seen


WEEKDAY_CRON = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@router.post("/plan", status_code=201)
def create_plan(
    payload: PlanIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Turn a date + time + repeat choice into a schedule.

    The Composer speaks in "every other day at 9am"; cron stays in here.
    """
    if (payload.target_id is None) == (payload.campaign_id is None):
        raise HTTPException(
            status_code=400, detail="Provide exactly one of target_id or campaign_id."
        )
    if payload.campaign_id is not None and session.get(Campaign, payload.campaign_id) is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    if payload.target_id is not None and session.get(Target, payload.target_id) is None:
        raise HTTPException(status_code=404, detail="Target not found.")

    try:
        hour, minute = (int(x) for x in payload.time.split(":")[:2])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"'{payload.time}' is not a valid time (use HH:MM)."
        ) from exc

    kind = ScheduleKind.cron
    cron_expr = f"{minute} {hour} * * *"
    run_at = None
    interval_days = None

    if payload.repeat == "once":
        if not payload.date:
            raise HTTPException(status_code=400, detail="Pick a date.")
        kind = ScheduleKind.once
        run_at = _parse_local(f"{payload.date}T{payload.time}")

    elif payload.repeat == "daily":
        cron_expr = f"{minute} {hour} * * *"

    elif payload.repeat == "weekdays":
        cron_expr = f"{minute} {hour} * * mon-fri"

    elif payload.repeat == "weekly":
        day = WEEKDAY_CRON[max(0, min(payload.weekday, 6))]
        cron_expr = f"{minute} {hour} * * {day}"

    elif payload.repeat in ("alternate", "custom"):
        days = 2 if payload.repeat == "alternate" else max(1, payload.interval_days)
        kind = ScheduleKind.interval
        interval_days = days
        # First run: the chosen date at the chosen time, or the next such time.
        start_local = _local_datetime(payload.date, hour, minute)
        run_at = start_local.astimezone(timezone.utc).replace(tzinfo=None)

    elif payload.repeat == "custom" and payload.cron_expr:
        cron_expr = _validate_cron(payload.cron_expr)

    if kind == ScheduleKind.cron:
        cron_expr = _validate_cron(cron_expr)

    row = Schedule(
        target_id=payload.target_id,
        campaign_id=payload.campaign_id,
        kind=kind,
        cron_expr=cron_expr,
        run_at=run_at,
        interval_days=interval_days,
        content_type=payload.content_type,
        active=True,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    scheduler.reload_jobs()
    return _serialise(row)


def _local_datetime(date_str: str | None, hour: int, minute: int) -> datetime:
    """Chosen date at the chosen time, rolled forward if already past."""
    now = datetime.now(config.tz)
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"'{date_str}' is not a valid date."
            ) from exc
        when = day.replace(hour=hour, minute=minute, tzinfo=config.tz)
    else:
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += timedelta(days=1)
    return when


@router.post("", status_code=201)
def create_schedule(
    payload: ScheduleIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if (payload.target_id is None) == (payload.campaign_id is None):
        raise HTTPException(
            status_code=400, detail="Provide exactly one of target_id or campaign_id."
        )
    if payload.target_id is not None and session.get(Target, payload.target_id) is None:
        raise HTTPException(status_code=404, detail="Target not found.")
    if payload.campaign_id is not None and session.get(Campaign, payload.campaign_id) is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    run_at = None
    cron_expr = payload.cron_expr
    if payload.kind in (ScheduleKind.once, ScheduleKind.interval):
        if not payload.run_at:
            raise HTTPException(status_code=400, detail="Pick a date and time.")
        run_at = _parse_local(payload.run_at)
    else:
        cron_expr = _validate_cron(cron_expr)

    row = Schedule(
        target_id=payload.target_id,
        campaign_id=payload.campaign_id,
        kind=payload.kind,
        cron_expr=cron_expr,
        run_at=run_at,
        interval_days=payload.interval_days,
        content_type=payload.content_type,
        active=payload.active,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    scheduler.reload_jobs()
    return _serialise(row)


@router.patch("/{schedule_id}")
def update_schedule(
    schedule_id: int, payload: SchedulePatch, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = session.get(Schedule, schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")

    data = payload.model_dump(exclude_unset=True)
    if "cron_expr" in data and data["cron_expr"]:
        data["cron_expr"] = _validate_cron(data["cron_expr"])
    if "run_at" in data and data["run_at"]:
        data["run_at"] = _parse_local(data["run_at"])

    for key, value in data.items():
        setattr(row, key, value)

    session.add(row)
    session.commit()
    session.refresh(row)
    scheduler.reload_jobs()
    return _serialise(row)


@router.delete("/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: int, session: Session = Depends(get_session)) -> None:
    row = session.get(Schedule, schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    session.delete(row)
    session.commit()
    scheduler.reload_jobs()


@router.get("/preview")
def preview(cron_expr: str) -> dict[str, Any]:
    """Human-readable preview for the schedule editor, before saving."""
    expr = _validate_cron(cron_expr)
    trigger = scheduler.parse_cron(expr)
    upcoming_runs: list[str] = []
    previous = None
    now = datetime.now(config.tz)
    for _ in range(3):
        nxt = trigger.get_next_fire_time(previous, previous or now)
        if nxt is None:
            break
        upcoming_runs.append(fmt_local(nxt))
        previous = nxt
    return {"cron_expr": expr, "human": describe(expr), "next_runs": upcoming_runs}
