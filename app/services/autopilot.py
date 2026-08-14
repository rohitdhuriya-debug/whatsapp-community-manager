"""Autopilot: plan -> write -> deliver, unattended.

Deliberately thin. All it does is decide what today's post should be and then
hand that to the Composer pipeline, so scheduling, previews, approval, sending
and logging stay in exactly one place.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlmodel import Session, select

from ..models import Autopilot, Campaign, Draft, DraftStatus, Target
from ..util import utcnow
from . import composer, planner, sender, waha

log = logging.getLogger(__name__)


class AutopilotError(RuntimeError):
    """A run failed. Message is safe to show in the UI."""


def backing_campaign(session: Session, autopilot: Autopilot) -> Campaign:
    campaign = session.get(Campaign, autopilot.campaign_id)
    if campaign is None:
        raise AutopilotError("This autopilot has lost its settings. Recreate it.")
    return campaign


async def run(
    session: Session,
    autopilot: Autopilot,
    *,
    deliver: bool,
    on_phase: Any = None,
) -> dict[str, Any]:
    """One full autopilot cycle.

    `deliver=False` stops after generating, so the dashboard can show a preview
    before anything is sent.
    """
    def phase(name: str, detail: str = "") -> None:
        if on_phase:
            on_phase(name, detail)

    campaign = backing_campaign(session, autopilot)
    if not composer.campaign_targets(session, campaign.id):
        raise AutopilotError("This autopilot has no chats selected.")

    # 1 · decide what to post
    phase("research", "Deciding what to post…")
    try:
        decision = await planner.plan(
            session, autopilot, campaign,
            on_note=lambda note: phase("research", note),
        )
    except planner.PlannerError as exc:
        raise AutopilotError(str(exc)) from exc

    log.info(
        "Autopilot %s planned: %r as %s", autopilot.id, decision["topic"][:70],
        decision["format"],
    )

    # 2 · write it, through the normal Composer path
    campaign.brief = planner.brief_from_plan(decision)
    campaign.output_type = planner.output_type_for(decision["format"])
    campaign.name = decision["topic"][:60]
    session.add(campaign)
    session.commit()

    # Retire outstanding approvals first: they were raised for the draft we
    # are about to delete, and would otherwise release the new one.
    from . import approvals as _approvals

    _approvals.supersede(session, campaign.id)

    # Clear any pending draft from an earlier run of this autopilot, so a
    # re-run does not stack up two drafts per chat.
    for draft in session.exec(
        select(Draft).where(
            Draft.campaign_id == campaign.id, Draft.status == DraftStatus.pending
        )
    ).all():
        session.delete(draft)
    session.commit()

    try:
        result = await composer.run_campaign(session, campaign, on_phase=on_phase)
    except composer.ComposerError as exc:
        raise AutopilotError(str(exc)) from exc

    # 3 · remember it, so tomorrow avoids it
    planner.remember(autopilot, decision)
    autopilot.last_run_at = utcnow()
    session.add(autopilot)
    session.commit()

    result["plan"] = {
        "topic": decision["topic"],
        "angle": decision["angle"],
        "format": decision["format"],
        "why": decision["why"],
        "repeat_warning": decision["repeat_warning"],
        # What it was built on, so the card can show this was real news.
        "headline": decision.get("headline", ""),
        "headline_url": decision.get("headline_url", ""),
        "headline_source": decision.get("headline_source", ""),
        "headlines_offered": decision.get("headlines_offered", 0),
    }

    if not deliver:
        result["delivered"] = False
        return result

    # 4 · send, unless approval is required
    if autopilot.approval_required:
        result["delivered"] = False
        result["awaiting_approval"] = True

        # Sign off from the phone rather than the dashboard. Autopilot fires
        # unattended, so this is the setting that makes it usable - otherwise a
        # draft sits in the browser until someone opens it.
        if (autopilot.approval_mode or "dashboard") == "whatsapp":
            from . import approvals

            phase("saving", "Sending for approval on WhatsApp…")
            raised = []
            for draft_id in result.get("draft_ids", []):
                draft = session.get(Draft, draft_id)
                if draft is None or draft.status != DraftStatus.pending:
                    continue
                target = session.get(Target, draft.target_id)
                if target is None:
                    continue
                try:
                    info = await approvals.request(session, campaign, [draft], target)
                except Exception as exc:
                    # A failed notification must not lose the draft.
                    result.setdefault("approval_error", str(exc))
                    continue
                raised.append({**info, "target": target.name})
            if raised:
                result["approvals"] = raised
        return result

    phase("saving", "Sending…")
    sent, failed = 0, []
    for draft_id in result["draft_ids"]:
        draft = session.get(Draft, draft_id)
        if draft is None:
            continue
        target = session.get(Target, draft.target_id)
        if target is None:
            continue
        try:
            await sender.send_draft(session, draft, target)
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
    session.commit()

    result["delivered"] = True
    result["sent"] = sent
    result["failed"] = failed
    return result


def for_campaign(session: Session, campaign_id: int) -> Autopilot | None:
    """The autopilot driving this campaign, if any."""
    return session.exec(
        select(Autopilot).where(
            Autopilot.campaign_id == campaign_id, Autopilot.active
        )
    ).first()
