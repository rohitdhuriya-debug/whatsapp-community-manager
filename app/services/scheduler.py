"""APScheduler wiring.

One cron job per active row in `schedules`, all evaluated in Asia/Kolkata (HC-5).
Jobs are hot-reloaded whenever schedules change, so the API never needs a restart.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import select

from ..config import config
from ..db import session_scope
from ..models import Campaign, Schedule, ScheduleKind, Target

log = logging.getLogger(__name__)

# A missed run (laptop asleep, app restarting) still fires if we notice within
# an hour; beyond that it is stale and gets skipped rather than spamming.
MISFIRE_GRACE_SECONDS = 3600

_scheduler: AsyncIOScheduler | None = None


def _job_id(schedule_id: int) -> str:
    return f"schedule-{schedule_id}"


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


NEWS_INTERVAL_MINUTES = 60


def start() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=config.tz)
        _scheduler.start()
        log.info("Scheduler started (timezone %s)", config.timezone)

        # Standing job, separate from user schedules: news costs no API quota,
        # so it can refresh hourly regardless of what else is configured.
        # start_date pulls the first run forward: apscheduler otherwise defaults
        # it to now + interval, so a fresh boot showed hour-old news for an hour.
        from datetime import datetime, timedelta

        _scheduler.add_job(
            refresh_news,
            trigger=IntervalTrigger(
                minutes=NEWS_INTERVAL_MINUTES,
                timezone=config.tz,
                start_date=datetime.now(config.tz) + timedelta(seconds=20),
            ),
            id="news-refresh",
            name="Refresh news feeds",
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
    reload_jobs()
    return _scheduler


async def refresh_news() -> None:
    """Hourly headline pull. Keyless, so it never touches the model budget."""
    from . import news

    try:
        with session_scope() as session:
            news.seed_defaults(session)
            result = await news.refresh_all(session)
            log.info(
                "News refresh: %s new item(s) across %s feed(s).",
                result["added"], result["feeds"],
            )
    except Exception:
        log.exception("News refresh failed")


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("Scheduler stopped.")


def parse_cron(expr: str) -> CronTrigger:
    """5-field cron -> trigger. Raises ValueError on a bad expression."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(
            f"Cron needs 5 fields (minute hour day month weekday), got {len(fields)}: '{expr}'"
        )
    minute, hour, day, month, day_of_week = fields
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone=config.tz,
    )


def reload_jobs() -> int:
    """Rebuild every job from the DB. Safe to call on any schedule change."""
    if _scheduler is None:
        return 0

    for job in _scheduler.get_jobs():
        if job.id.startswith("schedule-"):
            job.remove()

    registered = 0
    with session_scope() as session:
        rows = session.exec(select(Schedule).where(Schedule.active)).all()
        targets = {t.id: t for t in session.exec(select(Target)).all()}
        campaigns = {c.id: c for c in session.exec(select(Campaign)).all()}

        for row in rows:
            if row.campaign_id is not None:
                campaign = campaigns.get(row.campaign_id)
                if campaign is None:
                    continue
                func, args = run_scheduled_campaign, [row.campaign_id, row.id]
                label = campaign.name or (campaign.brief[:38] + "…")
                label = f"{label} · {campaign.output_type.value}"
            else:
                target = targets.get(row.target_id)
                if target is None or not target.enabled:
                    continue
                func, args = run_scheduled_job, [row.target_id, row.content_type.value]
                label = f"{target.name} · {row.content_type.value}"

            try:
                trigger = _trigger_for(row)
            except ValueError as exc:
                log.warning("Skipping schedule %s: %s", row.id, exc)
                continue
            if trigger is None:
                continue  # a one-off whose moment has already passed

            _scheduler.add_job(
                func,
                trigger=trigger,
                id=_job_id(row.id),
                args=args,
                name=label,
                misfire_grace_time=MISFIRE_GRACE_SECONDS,
                coalesce=True,       # one run after a gap, not a burst
                max_instances=1,
                replace_existing=True,
            )
            registered += 1

    log.info("Scheduler holds %s active job(s).", registered)
    return registered


def _trigger_for(row: Schedule):
    """One-off, every-N-days, or cron."""
    if row.kind == ScheduleKind.once:
        if row.run_at is None:
            raise ValueError("a one-off schedule needs a run_at")
        # run_at is stored naive UTC; APScheduler needs it aware.
        when = row.run_at.replace(tzinfo=timezone.utc).astimezone(config.tz)
        if when <= datetime.now(config.tz):
            return None
        return DateTrigger(run_date=when, timezone=config.tz)

    if row.kind == ScheduleKind.interval:
        days = row.interval_days or 2
        if days < 1:
            raise ValueError("interval_days must be at least 1")
        start = (
            row.run_at.replace(tzinfo=timezone.utc).astimezone(config.tz)
            if row.run_at else datetime.now(config.tz)
        )
        return IntervalTrigger(days=days, start_date=start, timezone=config.tz)

    return parse_cron(row.cron_expr)


def next_run_for_target(target_id: int | None) -> datetime | None:
    """Earliest upcoming run across all of this target's jobs."""
    if _scheduler is None or target_id is None:
        return None
    times = [
        job.next_run_time
        for job in _scheduler.get_jobs()
        if job.id.startswith("schedule-")
        and job.args
        and job.args[0] == target_id
        and job.next_run_time is not None
    ]
    return min(times) if times else None


async def run_scheduled_job(target_id: int, content_type: str) -> None:
    """Entry point for a per-target persona firing."""
    # Imported here so a pipeline import error can never stop the app booting.
    from . import pipeline

    try:
        await pipeline.run_for_target(target_id, content_type, jitter=True)
    except Exception:
        log.exception("Scheduled job failed for target %s (%s)", target_id, content_type)


async def run_scheduled_campaign(campaign_id: int, schedule_id: int) -> None:
    """Entry point for a scheduled firing: plan (if autopilot), write, send."""
    import asyncio
    import random

    from ..models import Draft, DraftStatus
    from ..util import utcnow
    from . import composer, sender, waha

    from . import autopilot as autopilot_service

    try:
        with session_scope() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                log.warning("Scheduled campaign %s no longer exists.", campaign_id)
                return

            # An autopilot decides its own topic and format each run, so it
            # owns the whole cycle including delivery.
            pilot = autopilot_service.for_campaign(session, campaign_id)
            if pilot is not None:
                await asyncio.sleep(random.randint(30, 120))  # FR-5 jitter
                outcome = await autopilot_service.run(session, pilot, deliver=True)
                log.info(
                    "Autopilot %s ran: %r (%s), delivered=%s",
                    pilot.id, outcome["plan"]["topic"][:60],
                    outcome["plan"]["format"], outcome.get("delivered"),
                )
                _retire_if_once(session, schedule_id)
                return

            # Drop any stale preview so the scheduled run sends fresh content.
            for old in session.exec(
                select(Draft).where(
                    Draft.campaign_id == campaign_id, Draft.status == DraftStatus.pending
                )
            ).all():
                session.delete(old)
            session.commit()

            result = await composer.run_campaign(session, campaign)

            # FR-5 jitter, so several campaigns in one window do not burst.
            await asyncio.sleep(random.randint(30, 120))

            from . import approvals

            for draft_id in result["draft_ids"]:
                draft = session.get(Draft, draft_id)
                if draft is None:
                    continue
                target = session.get(Target, draft.target_id)
                if target is None:
                    continue

                # A scheduled run used to generate and send in one go, ignoring
                # approval entirely - so a chat set to sign off on WhatsApp had
                # its post published before the phone even buzzed.
                mode = approvals.resolve_mode(campaign, target)
                if mode == "whatsapp":
                    try:
                        info = await approvals.request(
                            session, campaign, [draft], target
                        )
                        log.info(
                            "Scheduled campaign %s: %s awaiting approval %s",
                            campaign_id, target.name, info["code"],
                        )
                    except Exception as exc:
                        # Leave it pending rather than sending unapproved.
                        log.warning(
                            "Could not raise approval for %s: %s", target.name, exc
                        )
                    continue
                if target.approval_required:
                    log.info(
                        "Scheduled campaign %s: %s left pending for dashboard approval",
                        campaign_id, target.name,
                    )
                    continue

                try:
                    await sender.send_draft(session, draft, target)
                except (sender.SendBlocked, waha.WahaError) as exc:
                    draft.status = DraftStatus.failed
                    draft.error = getattr(exc, "message", str(exc))
                    session.add(draft)
                    continue
                draft.status = DraftStatus.sent
                draft.sent_at = utcnow()
                session.add(draft)
            session.commit()

            _retire_if_once(session, schedule_id)

    except Exception:
        log.exception("Scheduled campaign %s failed", campaign_id)
    finally:
        reload_jobs()


def _retire_if_once(session, schedule_id: int) -> None:
    """A one-off has done its job."""
    schedule = session.get(Schedule, schedule_id)
    if schedule is not None and schedule.kind == ScheduleKind.once:
        schedule.active = False
        session.add(schedule)
        session.commit()
