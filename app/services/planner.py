"""The thinking step: decide what to post today, before writing it.

An autopilot fires on a schedule with no human in the loop, so the risk is not
bad prose - it is repetition. Left alone, a model asked "write something for an
AI community" produces near-identical posts every day.

Two defences, because a prompt alone is not a guarantee:
  1. the planner is shown everything already sent and told to avoid it
  2. the returned topic is scored against that history in code, and a too-similar
     plan is rejected and re-planned with explicit exclusions

Format variety is handled the same way: the planner proposes, and the code
refuses a format used in the last couple of runs when alternatives exist.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from sqlmodel import Session

from ..config import config
from ..models import Autopilot, Campaign, Engine, OutputType
from . import engines, research

log = logging.getLogger(__name__)

# How much of the back catalogue the planner sees, and how much code checks.
HISTORY_SHOWN = 25
HISTORY_CHECKED = 40

# Jaccard overlap of significant words above which two topics count as the
# same idea. 0.5 catches "Top 10 AI tools for founders" vs
# "10 best AI tools founders should use" without blocking a genuine new angle.
SIMILARITY_LIMIT = 0.5

# Words that carry no topical meaning, so they should not make two unrelated
# topics look alike.
STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with", "your",
    "you", "how", "what", "why", "best", "top", "guide", "list", "using", "use",
    "that", "this", "from", "is", "are", "it", "its", "new", "today", "week",
    "should", "can", "will", "about", "into", "make", "made", "get", "need",
}

FORMAT_HINTS = {
    "message": "a short WhatsApp message - a single sharp insight or update",
    "pdf": "a multi-section PDF resource people will save - a guide or checklist",
    "excel": "a spreadsheet - a comparison, tracker, or dataset with real rows",
    "poll": "a poll question that gets the community arguing",
}


class PlannerError(RuntimeError):
    """Planning failed. Message is safe to show in the UI."""


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def _stem(word: str) -> str:
    """Crude singularisation.

    Without it "SIP checklist for beginners" and "a beginner checklist for
    starting SIPs" share only one word and score 0.17 - clearly the same idea
    slipping through. Folding plurals brings that to 0.75.
    """
    for suffix, keep in (("ies", 3), ("es", 2), ("s", 1)):
        if word.endswith(suffix) and len(word) - keep >= 3:
            if suffix == "ies":
                return word[:-3] + "y"
            if suffix == "es" and not word.endswith(("ses", "xes", "zes", "ches", "shes")):
                continue  # "tools" style, handled by the plain -s rule
            if suffix == "s" and word.endswith("ss"):
                return word
            return word[: -keep]
    return word


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {_stem(w) for w in words if len(w) > 2 and w not in STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of significant words. 1.0 = same idea."""
    ka, kb = _keywords(a), _keywords(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka | kb)


def is_repeat(topic: str, history: list[str]) -> str | None:
    """The most similar past topic, if one is too close."""
    for past in history[:HISTORY_CHECKED]:
        if similarity(topic, past) >= SIMILARITY_LIMIT:
            return past
    return None


def pick_format(plan_format: str, autopilot: Autopilot) -> str:
    """Honour a pinned format, else rotate away from what was just used."""
    if autopilot.format_mode != "auto":
        return autopilot.format_mode

    allowed = [f for f in (autopilot.formats or []) if f in FORMAT_HINTS]
    if not allowed:
        allowed = list(FORMAT_HINTS)

    recent = autopilot.recent_formats or []
    fresh = [f for f in allowed if f not in recent[:2]]
    pool = fresh or allowed

    if plan_format in pool:
        return plan_format
    # The planner asked for something it just used; take the least recent.
    return min(pool, key=lambda f: recent.index(f) if f in recent else -1)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


PLAN_SCHEMA = """{
  "topic": "the specific thing to post about today, one sentence",
  "angle": "what makes this worth reading - the hook or the takeaway",
  "format": "message | pdf | excel | poll",
  "why": "one line on why this is right for this community today"
}"""


def _build_messages(
    autopilot: Autopilot,
    campaign: Campaign,
    items: list[dict[str, Any]],
    exclude: list[str],
) -> list[dict[str, str]]:
    history = (autopilot.recent_topics or [])[:HISTORY_SHOWN]
    allowed = (
        [autopilot.format_mode]
        if autopilot.format_mode != "auto"
        else [f for f in (autopilot.formats or []) if f in FORMAT_HINTS] or list(FORMAT_HINTS)
    )

    system = "\n".join([
        "You are the editor of a WhatsApp community. Each day you decide what it should",
        "receive next. You are not writing the post - you are choosing what it is about.",
        "",
        f"THE COMMUNITY: {autopilot.context.strip() or 'a general interest community'}",
        "",
        "HOW YOU DECIDE:",
        "- Pick something genuinely useful this week, not evergreen filler.",
        "- Never repeat a topic already covered. A new angle on an old subject is fine",
        "  only if the takeaway is materially different.",
        "- Vary the shape of what you send. A community that gets the same format every",
        "  day stops opening it.",
        "- Prefer specifics: a named tool, a real number, a dated event, a concrete task.",
        "",
        "AVAILABLE FORMATS:",
        *[f"- {f}: {FORMAT_HINTS[f]}" for f in allowed],
        "",
        f"Respond with ONLY a JSON object, no code fence:\n{PLAN_SCHEMA}",
    ] + ([f"\nNEVER COVER: {autopilot.avoid.strip()}"] if autopilot.avoid.strip() else []))

    parts = [f"Today is {datetime.now(config.tz):%A, %d %B %Y}."]

    if history:
        parts.append(
            "ALREADY SENT — do not repeat any of these:\n"
            + "\n".join(f"- {t}" for t in history)
        )
    else:
        parts.append("Nothing has been sent yet. This is the first post.")

    if autopilot.recent_formats:
        parts.append(f"Recent formats used (newest first): {', '.join(autopilot.recent_formats[:5])}")

    if items:
        parts.append(
            "Fresh sources from the web today (use if genuinely relevant):\n"
            + research.format_for_prompt(items)
        )

    if exclude:
        parts.append(
            "Your previous suggestion was too close to something already sent. "
            "Do NOT propose anything like:\n" + "\n".join(f"- {t}" for t in exclude)
            + "\nPick a clearly different subject."
        )

    parts.append("Decide what this community should receive today.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


async def plan(
    session: Session,
    autopilot: Autopilot,
    campaign: Campaign,
    *,
    on_note: Any = None,
) -> dict[str, Any]:
    """Decide today's topic and format. Retries once if it repeats itself."""
    items: list[dict[str, Any]] = []
    if campaign.use_research:
        query = autopilot.context or campaign.brief or "news"
        items = await research.research(query, "news")

    history = autopilot.recent_topics or []
    exclude: list[str] = []

    for attempt in (1, 2):
        messages = _build_messages(autopilot, campaign, items, exclude)
        try:
            data, label = await engines.generate_json(
                session,
                Engine(campaign.engine),
                messages,
                model_override=campaign.model_override,
                on_note=on_note,
            )
        except engines.EngineError as exc:
            raise PlannerError(f"Could not plan today's post: {exc}") from exc

        topic = str(data.get("topic") or "").strip()
        if not topic:
            raise PlannerError("The planner returned no topic.")

        clash = is_repeat(topic, history)
        if clash and attempt == 1:
            log.info("Planned topic too close to %r; re-planning.", clash[:60])
            if on_note:
                on_note("Too similar to a previous post — thinking again…")
            exclude = [clash, topic]
            continue

        chosen_format = pick_format(str(data.get("format") or "").strip().lower(), autopilot)
        if clash:
            # Second attempt still overlapped. Go ahead but say so, rather than
            # skipping the day's post entirely.
            log.warning("Proceeding with a topic similar to %r", clash[:60])

        return {
            "topic": topic,
            "angle": str(data.get("angle") or "").strip(),
            "format": chosen_format,
            "why": str(data.get("why") or "").strip(),
            "repeat_warning": clash or "",
            "sources": len(items),
            "model_used": label,
            "research": items,
        }

    raise PlannerError("Planning failed.")


def brief_from_plan(plan_result: dict[str, Any]) -> str:
    """Turn the plan into the brief the Composer already knows how to run."""
    parts = [plan_result["topic"]]
    if plan_result.get("angle"):
        parts.append(f"Angle: {plan_result['angle']}")
    return "\n".join(parts)


def remember(autopilot: Autopilot, plan_result: dict[str, Any]) -> None:
    """Record what went out so the next run can avoid it."""
    topics = [plan_result["topic"], *(autopilot.recent_topics or [])]
    autopilot.recent_topics = topics[:HISTORY_CHECKED]

    formats = [plan_result["format"], *(autopilot.recent_formats or [])]
    autopilot.recent_formats = formats[:10]

    autopilot.last_plan = {
        k: plan_result.get(k) for k in ("topic", "angle", "format", "why", "model_used")
    }


def output_type_for(name: str) -> OutputType:
    try:
        return OutputType(name)
    except ValueError:
        return OutputType.message
