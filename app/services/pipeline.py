"""research -> draft -> queue or send (FR-1 … FR-5)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any

from sqlmodel import Session

from ..db import session_scope
from ..models import ContentType, Draft, DraftStatus, Language, Target
from ..util import utcnow
from . import llm, research, sender, waha

log = logging.getLogger(__name__)

MAX_CHARS = 800

# FR-5: when several targets fire in the same window, spread the sends out.
JITTER_MIN_SECONDS = 30
JITTER_MAX_SECONDS = 120
RETRY_DELAY_SECONDS = 60

FINANCE_DISCLAIMER = "Educational purpose only, not investment advice."

# Word-boundary matched on purpose. A plain substring test fires on "marketers"
# (because of "market") and stamps an AI-tools channel with a SEBI-style
# investment disclaimer, which is both wrong and confusing.
FINANCE_PATTERN = re.compile(
    r"\b("
    r"invest(?:ing|ment|ments|or|ors)?"
    r"|stocks?|equit(?:y|ies)|shares?"
    r"|mutual\s+funds?|sips?"
    r"|trad(?:e|es|er|ers|ing)"
    # Bare "market" is too weak - it fires on "product market fit". Only count
    # it when qualified. Real finance briefs also hit stock/nifty/sensex below.
    r"|(?:stock|share|equity|commodity|bond|capital|forex|derivatives?)\s+markets?"
    r"|market\s+cap|bull\s+market|bear\s+market"
    r"|financ(?:e|es|ial)"
    r"|nifty|sensex|bse|nse|ipos?"
    r"|portfolios?|wealth|dividends?"
    r"|crypto\w*|bitcoin"
    r")\b",
    re.IGNORECASE,
)


class PipelineError(RuntimeError):
    """Anything that stopped a draft from being produced."""


# ---------------------------------------------------------------------------
# Prompt construction (FR-2)
# ---------------------------------------------------------------------------


def _is_finance(target: Target) -> bool:
    """Whether to append the investment disclaimer.

    Auto-detection is a heuristic and this decision has compliance weight, so
    the target can override it outright.
    """
    mode = (getattr(target, "disclaimer_mode", None) or "auto").lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    blob = f"{target.niche} {target.persona_prompt} {target.research_instructions}"
    return FINANCE_PATTERN.search(blob) is not None


def _format_rules(target: Target, content_type: ContentType) -> str:
    language = (
        "Hinglish - natural Roman-script Hindi-English mix, the way Indians actually "
        "message each other. Not pure Hindi, not pure English."
        if target.language == Language.hinglish
        else "English."
    )

    rules = [
        "FORMATTING RULES - these are absolute:",
        "- WhatsApp formatting only: *bold*, _italic_. Single asterisks, never double.",
        "- No markdown headers (#), no bullet characters like '-' at line start, no hashtags.",
        f"- Hard maximum {MAX_CHARS} characters. Shorter is better.",
        "- First line is a hook that makes someone stop scrolling.",
        "- One idea per message. Do not cram.",
        f"- Write in {language}",
        "- At most ONE link in the whole message.",
        "- Never mention that you are an AI or that this was generated.",
        "- Do not add a signature, a title, or a preamble like 'Here is your message'.",
        "- Output ONLY the message text, nothing else.",
    ]

    if target.cta_link:
        rules.append(f"- When a call to action fits naturally, use this link: {target.cta_link}")
    else:
        rules.append("- No CTA link is configured, so do not invent one.")

    if target.tone:
        rules.append(f"- Tone: {target.tone}")
    if target.banned_topics:
        rules.append(f"- NEVER mention these topics: {target.banned_topics}")
    if _is_finance(target):
        rules.append(
            f"- This is a finance audience. End the message with exactly this line: {FINANCE_DISCLAIMER}"
        )
        rules.append("- Never give price targets, guaranteed returns, or buy/sell calls.")

    intent = {
        ContentType.news: "Share what is genuinely new and why it matters to this audience.",
        ContentType.tip: "Give one practical tip they can act on today.",
        ContentType.resource: "Recommend one genuinely useful resource and say who it is for.",
        ContentType.poll: "Write a poll question that sparks opinion.",
    }[content_type]
    rules.append(f"- Purpose of this message: {intent}")

    return "\n".join(rules)


def build_messages(
    target: Target, items: list[dict[str, Any]], content_type: ContentType
) -> list[dict[str, str]]:
    persona = target.persona_prompt.strip() or (
        f"You write short, useful WhatsApp messages for a community about {target.niche or 'this topic'}."
    )

    system = f"{persona}\n\n{_format_rules(target, content_type)}"
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]

    # Few-shot: my own best past messages, so drafts sound like me.
    for example in (target.example_messages or [])[:4]:
        example = example.strip()
        if example:
            messages.append({"role": "user", "content": "Write the next message."})
            messages.append({"role": "assistant", "content": example})

    research_block = research.format_for_prompt(items)
    if content_type == ContentType.poll:
        task = (
            f"Today is {research.today_label()}.\n\n"
            f"Recent sources:\n{research_block}\n\n"
            "Write a poll for this community. Respond with ONLY a JSON object, no code fence:\n"
            '{"question": "...", "options": ["...", "...", "..."]}\n'
            "3 or 4 options, each under 25 characters. The question must follow every "
            "formatting rule above."
        )
    else:
        task = (
            f"Today is {research.today_label()}.\n\n"
            f"Recent sources:\n{research_block}\n\n"
            "Write today's message for this community, following every formatting rule. "
            "Output only the message text."
        )

    messages.append({"role": "user", "content": task})
    return messages


# ---------------------------------------------------------------------------
# Output hygiene
#
# Free models drift from instructions, so the rules are enforced here rather
# than merely requested in the prompt.
# ---------------------------------------------------------------------------


def sanitize(text: str, target: Target) -> str:
    text = text.strip()

    # Strip a wrapping code fence if the model added one.
    fence = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # Drop a leading "Here is ..." preamble line.
    text = re.sub(
        r"^(here(?:'s| is)[^\n:]*:|sure[^\n:]*:|draft[^\n:]*:)\s*\n+",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Hashtags go first: otherwise the header rule below eats the leading '#'
    # of "#AI" and leaves a stray "AI" behind.
    text = re.sub(r"(?<!\w)#\w+", "", text)
    # Headers require whitespace after the hashes, so "#tag" is never a header.
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.DOTALL)    # **b** -> *b*
    text = re.sub(r"__(.+?)__", r"_\1_", text, flags=re.DOTALL)
    text = re.sub(r"^\s*[-*+]\s+", "• ", text, flags=re.MULTILINE)     # md bullets
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()

    text = _enforce_single_link(text, target)

    if _is_finance(target):
        without = text.replace(FINANCE_DISCLAIMER, "").rstrip()
        text = f"{without}\n\n{FINANCE_DISCLAIMER}"

    return _truncate_cleanly(text)


def _enforce_single_link(text: str, target: Target) -> str:
    """Keep at most one URL - the CTA if configured, else the first one."""
    urls = re.findall(r"https?://\S+", text)
    if len(urls) <= 1:
        return text

    keep = target.cta_link if target.cta_link in urls else urls[0]
    seen = False
    out = text
    for url in urls:
        if url == keep and not seen:
            seen = True
            continue
        out = out.replace(url, "", 1)

    # Deleting a URL mid-sentence strands its connectors ("... and also and http://").
    out = re.sub(r"\b(and|also|or|plus)(\s+(and|also|or|plus))+\b", r"\1", out, flags=re.I)
    out = re.sub(r"[:,]\s*(and|also|or|plus)\s+", ": ", out, flags=re.I)
    out = re.sub(r"\s+(and|also|or|plus)\s*$", "", out, flags=re.I | re.M)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r"[ \t]+([.,!?])", r"\1", out)


def _truncate_cleanly(text: str) -> str:
    """Trim to the limit on a sentence or line boundary, never mid-word."""
    if len(text) <= MAX_CHARS:
        return text

    tail = ""
    if text.rstrip().endswith(FINANCE_DISCLAIMER):
        tail = f"\n\n{FINANCE_DISCLAIMER}"
        text = text.rstrip()[: -len(FINANCE_DISCLAIMER)].rstrip()

    budget = MAX_CHARS - len(tail)
    clipped = text[:budget]
    for boundary in ("\n\n", ". ", "\n", " "):
        cut = clipped.rfind(boundary)
        if cut > budget * 0.6:
            clipped = clipped[:cut]
            break
    return clipped.rstrip().rstrip(",;:-") + tail


def parse_poll(raw: str) -> tuple[str, list[str]]:
    """Pull {question, options} out of the model's reply."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise PipelineError("The model did not return poll JSON. Try composing again.")

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Poll JSON was malformed: {exc}") from exc

    question = str(data.get("question") or data.get("name") or "").strip()
    options = [str(o).strip() for o in (data.get("options") or []) if str(o).strip()]

    if not question:
        raise PipelineError("Poll JSON had no question.")
    if len(options) < 2:
        raise PipelineError("A poll needs at least 2 options.")

    # WhatsApp caps poll options at 12; keep it tight regardless.
    options = [o[:100] for o in options[:4]]
    return question, options


# ---------------------------------------------------------------------------
# Draft (FR-2)
# ---------------------------------------------------------------------------


async def compose(
    session: Session, target: Target, content_type: ContentType
) -> Draft:
    """Research, draft, and persist a pending Draft. One model call."""
    items = await research.research(target.research_instructions, content_type.value)
    messages = build_messages(target, items, content_type)
    model = llm.resolve_model(session, target.model_override)
    api_key = llm.resolve_api_key(session)

    log.info("Drafting for '%s' with %s", target.name, model)
    try:
        raw, model_used = await llm.chat_with_fallback(model, messages, api_key=api_key)
    except llm.LLMError as exc:
        raise PipelineError(str(exc)) from exc
    finally:
        # Count the attempt either way - a failed call still spends free quota.
        llm.record_call(session)

    if model_used != model:
        log.info("Draft for '%s' used fallback model %s", target.name, model_used)

    poll_options = None
    if content_type == ContentType.poll:
        question, poll_options = parse_poll(raw)
        content = sanitize(question, target)
    else:
        content = sanitize(raw, target)

    if not content.strip():
        raise PipelineError("The model returned an empty message after cleanup.")

    draft = Draft(
        target_id=target.id,
        content_type=content_type,
        research_json=items,
        content=content,
        poll_options=poll_options,
        status=DraftStatus.pending,
        created_at=utcnow(),
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft


# ---------------------------------------------------------------------------
# Send with jitter + retry (FR-4, FR-5)
# ---------------------------------------------------------------------------


async def deliver(
    session: Session, target: Target, draft: Draft, *, jitter: bool = False
) -> Draft:
    """Send a draft. One retry after 60s, then mark failed. Always logged."""
    if jitter:
        delay = random.randint(JITTER_MIN_SECONDS, JITTER_MAX_SECONDS)
        log.info("Jitter: waiting %ss before sending to '%s'", delay, target.name)
        await asyncio.sleep(delay)

    for attempt in (1, 2):
        try:
            await sender.send_draft(session, draft, target)
        except sender.SendBlocked as exc:
            # A guard refused it; retrying cannot help.
            draft.status = DraftStatus.failed
            draft.error = str(exc)
            session.add(draft)
            session.commit()
            log.warning("Send blocked for '%s': %s", target.name, exc)
            return draft
        except waha.WahaError as exc:
            message = f"{exc.message} {exc.hint}".strip()
            if attempt == 1:
                log.warning(
                    "Send to '%s' failed (%s). Retrying in %ss.",
                    target.name, exc.message, RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue
            draft.status = DraftStatus.failed
            draft.error = message
            session.add(draft)
            session.commit()
            log.error("Send to '%s' failed twice: %s", target.name, message)
            return draft

        draft.status = DraftStatus.sent
        draft.sent_at = utcnow()
        draft.error = None
        session.add(draft)
        session.commit()
        return draft

    return draft


# ---------------------------------------------------------------------------
# Orchestration (FR-3)
# ---------------------------------------------------------------------------


async def queue_or_send(
    session: Session, target: Target, draft: Draft, *, jitter: bool = False
) -> Draft:
    if target.approval_required:
        log.info("Draft %s queued for approval ('%s').", draft.id, target.name)
        return draft
    return await deliver(session, target, draft, jitter=jitter)


async def run_for_target(
    target_id: int, content_type: str = "news", *, jitter: bool = False
) -> dict[str, Any]:
    """Full pipeline for one target. Used by both the scheduler and Compose now."""
    with session_scope() as session:
        target = session.get(Target, target_id)
        if target is None:
            raise PipelineError(f"Target {target_id} no longer exists.")
        if not target.enabled:
            raise PipelineError(f"'{target.name}' is paused.")

        try:
            kind = ContentType(content_type)
        except ValueError as exc:
            raise PipelineError(f"Unknown content type '{content_type}'.") from exc

        draft = await compose(session, target, kind)
        draft = await queue_or_send(session, target, draft, jitter=jitter)

        return {
            "draft_id": draft.id,
            "status": draft.status.value,
            "sources": len(draft.research_json or []),
            "characters": len(draft.content),
            "error": draft.error,
        }
