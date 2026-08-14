"""The Composer: one brief -> one piece of content -> many chats.

Deliberately generates once and fans the same output out to every selected
chat. A broadcast costs one model call regardless of how many communities it
goes to, which keeps the free tier viable.

Per-target personas (services/pipeline.py) remain the path for hands-off
recurring drafts; this is the deliberate, ad-hoc send.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from ..models import (
    Campaign,
    CampaignTarget,
    Draft,
    DraftStatus,
    Engine,
    OutputType,
    Target,
)
from ..util import utcnow
from . import assets, cover, drive, engines, research, sender
from .pipeline import MAX_CHARS, apply_disclaimer, parse_poll, sanitize

log = logging.getLogger(__name__)


class ComposerError(RuntimeError):
    """Generation failed. Message is safe to show in the UI."""


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------


def language_rule(language: str) -> str:
    key = (language or "").strip().lower()
    if key in ("hinglish", "hindi-english", "roman hindi"):
        return (
            "Write in Hinglish - a natural Roman-script mix of Hindi and English, the way "
            "Indians actually message each other. Not pure Hindi, not pure English, and "
            "never Devanagari script."
        )
    if key in ("", "english", "en"):
        return "Write in clear, natural English."
    if key == "hindi":
        return "Write in Hindi, using Devanagari script."
    return f"Write in {language.strip()}."


def _audience_line(targets: list[Target]) -> str:
    niches = sorted({t.niche.strip() for t in targets if t.niche.strip()})
    if not niches:
        return "The audience is a WhatsApp community."
    if len(niches) == 1:
        return f"The audience is a WhatsApp community about {niches[0]}."
    return "The audience spans these communities: " + ", ".join(niches) + "."


def _research_block(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    return (
        f"\n\nToday is {research.today_label()}. Recent sources you may draw on "
        f"(use only what is genuinely relevant, never invent facts):\n"
        f"{research.format_for_prompt(items)}"
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def build_message_messages(
    campaign: Campaign, targets: list[Target], items: list[dict[str, Any]]
) -> list[dict[str, str]]:
    cta = next((t.cta_link for t in targets if t.cta_link), "")
    banned = sorted({t.banned_topics.strip() for t in targets if t.banned_topics.strip()})
    # Channels are a broadcast feed with no replies threading underneath, so
    # the post itself has to carry the structure a conversation would.
    to_channel = any(is_channel(t.chat_id) for t in targets)

    system = "\n".join(
        [
            "You write short, high-signal WhatsApp broadcast messages.",
            _audience_line(targets),
            "",
            "FORMATTING RULES - absolute:",
            "- WhatsApp formatting only: *bold*, _italic_. Single asterisks, never double.",
            "- No markdown headers, no '#' headings, no hashtags.",
            f"- Hard maximum {MAX_CHARS} characters. Shorter is better.",
            "- First line is a hook that stops the scroll.",
            "- One idea per message.",
            "- At most ONE link in the whole message.",
            f"- {language_rule(campaign.language)}",
            "- Never mention that you are an AI or that this was generated.",
            "- Output ONLY the message text. No preamble, no title, no sign-off.",
        ]
        + ([
            "",
            "THIS GOES TO A CHANNEL. Make it scannable and alive:",
            "- Open with ONE emoji and a bold hook line. Never a wall of text.",
            "- Blank line between every block. Long paragraphs do not get read.",
            "- Use 3-5 short lines each led by an emoji (▪️ ✅ 📌 ⚡ 💡 🔹) for the",
            "  substance. One line, one point, under 90 characters each.",
            "- Bold the words that carry the meaning, not whole sentences.",
            "- Close with ONE question or invitation that asks for a reaction or a",
            "  reply. Make it specific and easy to answer.",
            "- 4 to 8 emoji in total. Enough to give it rhythm, never decoration",
            "  on every line, and never two in a row.",
        ] if to_channel else [
            "- No bullet characters at line start.",
        ])
        + ([f"- When a CTA fits naturally, use this link: {cta}"] if cta else
           ["- No CTA link is configured, so do not invent one."])
        + ([f"- NEVER mention: {'; '.join(banned)}"] if banned else [])
        + ([campaign.extra_instructions] if campaign.extra_instructions.strip() else [])
    )

    user = f"Brief: {campaign.brief.strip()}{_research_block(items)}\n\nWrite the message."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_poll_messages(
    campaign: Campaign, targets: list[Target], items: list[dict[str, Any]]
) -> list[dict[str, str]]:
    system = "\n".join([
        "You write WhatsApp polls for a community.",
        _audience_line(targets),
        language_rule(campaign.language),
        "Respond with ONLY a JSON object, no code fence, no commentary.",
    ])
    user = (
        f"Brief: {campaign.brief.strip()}{_research_block(items)}\n\n"
        'Return exactly: {"question": "...", "options": ["...", "...", "..."]}\n'
        "3 or 4 options, each under 25 characters. The question must be under 250 characters."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


PDF_SCHEMA = """{
  "title": "short document title",
  "subtitle": "one line saying who it is for and what they get",
  "sections": [
    {
      "heading": "section heading",
      "body": "one or two short paragraphs of plain prose",
      "bullets": ["concrete point", "another point"],
      "table": {"columns": ["Col A", "Col B"], "rows": [["v1", "v2"]]}
    }
  ],
  "footer_note": "one-line closing note or disclaimer",
  "caption": "the WhatsApp message that accompanies the file, under 400 characters"
}"""


def build_pdf_messages(
    campaign: Campaign, targets: list[Target], items: list[dict[str, Any]]
) -> list[dict[str, str]]:
    system = "\n".join([
        "You create genuinely useful, well-structured PDF resources for a WhatsApp community.",
        _audience_line(targets),
        language_rule(campaign.language),
        "",
        "QUALITY BAR:",
        "- Every section must carry real, specific information. No filler, no 'introduction to'",
        "  padding, no restating the title.",
        "- Prefer concrete numbers, names, steps and examples over general advice.",
        "- 4 to 7 sections. Each `body` is 1-2 short paragraphs.",
        "- Use `bullets` for scannable lists and `table` only when data is genuinely tabular.",
        "- `table` is optional per section; omit it unless it earns its place.",
        "- Plain text only inside the JSON: no markdown, no asterisks, no '#'.",
        "- Write money as 'Rs. 1,500', never the ₹ symbol, and use no emoji: the PDF",
        "  fonts cannot draw either.",
        "",
        "Respond with ONLY a JSON object matching this shape, no code fence:",
        PDF_SCHEMA,
    ] + ([campaign.extra_instructions] if campaign.extra_instructions.strip() else []))

    user = (
        f"Brief: {campaign.brief.strip()}{_research_block(items)}\n\n"
        "Create the PDF resource."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


EXCEL_SCHEMA = """{
  "title": "short workbook title",
  "sheets": [
    {
      "name": "tab name, max 31 chars",
      "title": "heading shown in the first cell",
      "columns": ["Column A", "Column B", "Column C"],
      "rows": [["value", "value", "value"]],
      "notes": "optional one-line note under the table"
    }
  ],
  "caption": "the WhatsApp message that accompanies the file, under 400 characters"
}"""


def build_excel_messages(
    campaign: Campaign, targets: list[Target], items: list[dict[str, Any]]
) -> list[dict[str, str]]:
    system = "\n".join([
        "You create genuinely useful spreadsheets for a WhatsApp community.",
        _audience_line(targets),
        language_rule(campaign.language),
        "",
        "QUALITY BAR:",
        "- Aim for 12-30 data rows unless the brief implies otherwise. Real, specific entries.",
        "- Every row must be complete - never leave a cell blank to pad the table.",
        "- Keep numbers as bare numerals (1500, not 'Rs. 1,500/-') so they stay sortable.",
        "  Put units in the column heading instead, e.g. 'Price (INR)'.",
        "- 3 to 6 columns. Headings short and specific.",
        "- One sheet unless the brief clearly needs more.",
        "- Plain text only: no markdown, no asterisks.",
        "",
        "Respond with ONLY a JSON object matching this shape, no code fence:",
        EXCEL_SCHEMA,
    ] + ([campaign.extra_instructions] if campaign.extra_instructions.strip() else []))

    user = (
        f"Brief: {campaign.brief.strip()}{_research_block(items)}\n\n"
        "Create the spreadsheet."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def campaign_targets(session: Session, campaign_id: int) -> list[Target]:
    links = session.exec(
        select(CampaignTarget).where(CampaignTarget.campaign_id == campaign_id)
    ).all()
    ids = [link.target_id for link in links]
    if not ids:
        return []
    rows = session.exec(select(Target).where(Target.id.in_(ids))).all()
    return list(rows)


CHANNEL_SUFFIX = "@newsletter"


def is_channel(chat_id: str) -> bool:
    return chat_id.endswith(CHANNEL_SUFFIX)


def _link_label(output: OutputType) -> str:
    return "Free PDF" if output == OutputType.pdf else "Download the sheet"


def _link_line(output: OutputType, link: str) -> str:
    """The download line as it appears in a channel post.

    Its own block with an emoji, so it reads as a deliberate call to action
    rather than a URL trailing off the end of the last sentence.
    """
    icon = "📄" if output == OutputType.pdf else "📊"
    return f"{icon} *{_link_label(output)}:*\n{link}"


def _short_brand(campaign: Campaign, session: Session | None) -> str:
    """A wordmark, not a headline.

    Autopilot sets campaign.name to the day's topic, so using it raw put a
    whole sentence across the top of the cover and through the badge.
    """
    name = (campaign.name or "").strip()
    if name and len(name) <= 22:
        return name
    # Too long to be a brand: use the community's name instead.
    if session is not None and campaign.id:
        targets = campaign_targets(session, campaign.id)
        if targets:
            return targets[0].name.strip()[:22]
    return " ".join(name.split()[:2])[:22]


def _recent_palettes(session: Session, limit: int = 2) -> list[str]:
    """Palettes of the last few covers, so consecutive posts differ."""
    rows = session.exec(
        select(Draft.image_path)
        .where(Draft.image_path.is_not(None))
        .order_by(Draft.id.desc())
        .limit(limit * 3)
    ).all()
    out: list[str] = []
    for path in rows:
        for name in cover.PALETTES:
            if path and f"-{name}-" in str(path) and name not in out:
                out.append(name)
    return out[:limit]


def _build_cover(
    campaign: Campaign,
    content: str,
    *,
    payload: dict[str, Any] | None = None,
    asset_path: str | None = None,
    has_link: bool = False,
    session: Session | None = None,
) -> str:
    """A designed cover PNG on disk, used as the channel post's image.

    Content-driven, so two posts do not come out looking like the same picture:
    the layout follows the shape of the text and the palette is derived from it.
    Falls back to page one of the PDF, and then to nothing - a cover is a
    nicety, and must never cost the post.
    """
    try:
        spec = cover.spec_from_content(
            content,
            brand=_short_brand(campaign, session),
            badge=_cover_badge(campaign.output_type),
            payload=payload,
            has_link=has_link,
            avoid_palettes=_recent_palettes(session) if session is not None else [],
        )
        palette = spec.palette or cover.pick_palette(
            f"{spec.headline}|{spec.body}|{spec.quote}", spec.avoid_palettes
        )
        spec.palette = palette
        stem = f"{assets.slugify(campaign.name or 'post', 'post')[:36]}-{palette}"
        path, _name = cover.build_cover(spec, stem=stem)
        return str(path)
    except Exception as exc:
        log.warning("Cover generation failed, falling back: %s", exc)

    if asset_path:
        rendered = assets.cover_image(Path(asset_path))
        if rendered:
            png, name = rendered
            path = assets.ASSET_DIR / name
            path.write_bytes(png)
            return str(path)
    return ""


def _cover_badge(output: OutputType) -> str:
    return {
        OutputType.pdf: "Free PDF",
        OutputType.excel: "Free sheet",
        OutputType.poll: "Your take",
    }.get(output, "New")


def _fallback_caption(campaign: Campaign, payload: dict[str, Any], kind: str) -> str:
    title = str(payload.get("title") or "").strip()
    if title:
        return f"*{title}*\n\n{campaign.brief.strip()[:300]}"
    return f"Here's the {kind} you asked for.\n\n{campaign.brief.strip()[:300]}"


async def run_campaign(
    session: Session,
    campaign: Campaign,
    on_phase: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Generate once and queue a pending draft for every selected chat.

    `on_phase(name, detail)` reports progress so the UI can show a real
    phase-driven bar instead of a spinner.
    """
    def phase(name: str, detail: str = "") -> None:
        if on_phase:
            on_phase(name, detail)

    targets = campaign_targets(session, campaign.id)
    if not targets:
        raise ComposerError("Pick at least one chat before generating.")
    if not campaign.brief.strip():
        raise ComposerError("Write a brief first.")


    items: list[dict[str, Any]] = []
    if campaign.use_research:
        phase("research")
        items = await research.research(campaign.brief, "news")
        phase("research", f"{len(items)} sources found")

    engine = campaign.engine
    output = campaign.output_type
    asset_path = asset_name = asset_mime = None
    poll_options = None
    # Only the pdf/excel branches produce one; the cover builder reads it for
    # a title and chips, so it must exist for message and poll runs too.
    payload: dict[str, Any] = {}

    phase("drafting")
    try:
        if output == OutputType.message:
            messages = build_message_messages(campaign, targets, items)
            raw, label = await engines.generate(
                session, engine, messages, model_override=campaign.model_override,
                on_note=lambda note: phase("drafting", note),
            )
            content = sanitize(raw, targets[0])

        elif output == OutputType.poll:
            messages = build_poll_messages(campaign, targets, items)
            raw, label = await engines.generate(
                session, engine, messages, model_override=campaign.model_override,
                on_note=lambda note: phase("drafting", note),
            )
            question, poll_options = parse_poll(raw)
            content = sanitize(question, targets[0])

        elif output in (OutputType.pdf, OutputType.excel):
            builder = build_pdf_messages if output == OutputType.pdf else build_excel_messages
            payload, label = await engines.generate_json(
                session, engine, builder(campaign, targets, items),
                model_override=campaign.model_override,
                on_note=lambda note: phase("drafting", note),
            )
            phase("building")
            brand = campaign.name.strip() or targets[0].name
            if output == OutputType.pdf:
                path, filename = assets.build_pdf(payload, brand=brand)
                asset_mime = assets.PDF_MIME
            else:
                path, filename = assets.build_excel(payload, brand=brand)
                asset_mime = assets.XLSX_MIME
            asset_path, asset_name = str(path), filename
            caption = str(payload.get("caption") or "").strip()
            content = sanitize(caption, targets[0]) if caption else _fallback_caption(
                campaign, payload, "PDF" if output == OutputType.pdf else "spreadsheet"
            )
        else:
            raise ComposerError(f"Unsupported output type '{output}'.")

    except engines.EngineError as exc:
        raise ComposerError(str(exc)) from exc

    if not content.strip() and output in (OutputType.message, OutputType.poll):
        raise ComposerError("The model returned an empty message after cleanup.")

    phase("saving")

    # Channels do not render document attachments reliably, so a channel post
    # is a picture of page one plus a link to the file. That needs the file
    # hosted somewhere, which is what Drive is for.
    channels = [t for t in targets if is_channel(t.chat_id)]
    drive_link = ""
    drive_error = ""
    cover_path = ""

    if asset_path and channels:
        phase("building", "Uploading to Google Drive…")
        try:
            uploaded = await drive.upload_asset(
                session, Path(asset_path),
                mimetype=asset_mime or "application/octet-stream",
                name=asset_name,
            )
            drive_link = uploaded["link"]
        except drive.DriveError as exc:
            # A missing link must never cost the whole post - the image and
            # caption still go out, and the reason is shown on the preview.
            drive_error = str(exc)
            log.warning("Drive upload failed: %s", exc)

    # Every channel post leads with a cover, whether or not it carries a file.
    # Without one WhatsApp scrapes any link in the text and draws its own card -
    # a Drive spreadsheet link renders as a blurry "Loading Google Sheets" tile.
    # Built after the upload so the CTA can reflect whether a link exists.
    if channels and campaign.generate_cover:
        phase("building", "Designing the cover image…")
        cover_path = _build_cover(
            campaign, content, payload=payload, asset_path=asset_path,
            has_link=bool(drive_link), session=session,
        )

    drafts: list[Draft] = []
    for target in targets:
        to_channel = is_channel(target.chat_id)

        if to_channel and (asset_path or cover_path):
            # Image + caption-with-link. No document attached.
            # Per target: one message is generated for every chat, but the
            # disclaimer decision is per target's own mode.
            body = apply_disclaimer(content, target)
            if drive_link:
                body = f"{body}\n\n{_link_line(output, drive_link)}"
            draft = Draft(
                target_id=target.id,
                campaign_id=campaign.id,
                content_type=research_content_type(output),
                output_type=OutputType.message,
                research_json=items,
                content=body,
                asset_path=None,
                asset_filename=None,
                asset_mime=None,
                image_path=cover_path or None,
                image_mime="image/png" if cover_path else None,
                drive_link=drive_link or None,
                engine_used=engine.value,
                model_used=label,
                status=DraftStatus.pending,
                created_at=utcnow(),
            )
        else:
            draft = Draft(
                target_id=target.id,
                campaign_id=campaign.id,
                content_type=research_content_type(output),
                output_type=output,
                research_json=items,
                # A poll question is not prose and must not grow a footer.
                content=(content if output == OutputType.poll
                         else apply_disclaimer(content, target)),
                poll_options=poll_options,
                asset_path=asset_path,
                asset_filename=asset_name,
                asset_mime=asset_mime,
                drive_link=drive_link or None,
                engine_used=engine.value,
                model_used=label,
                status=DraftStatus.pending,
                created_at=utcnow(),
            )

        session.add(draft)
        drafts.append(draft)

    campaign.last_run_at = utcnow()
    session.add(campaign)
    session.commit()
    for draft in drafts:
        session.refresh(draft)

    log.info(
        "Campaign %s generated %s (%s) for %s chat(s) via %s",
        campaign.id, output.value, label, len(drafts), engine.value,
    )
    asset_preview = None
    if asset_path:
        asset_preview = assets.preview(asset_path, output.value)

    return {
        "campaign_id": campaign.id,
        "output_type": output.value,
        "engine": engine.value,
        "model_used": label,
        "sources": len(items),
        "research": items,
        "content": content,
        "poll_options": poll_options,
        "asset_filename": asset_name,
        "asset_size": Path(asset_path).stat().st_size if asset_path else None,
        "asset_preview": asset_preview,
        "drive_link": drive_link,
        "drive_error": drive_error,
        "channel_count": len(channels),
        "cover_image": Path(cover_path).name if cover_path else None,
        "draft_ids": [d.id for d in drafts],
        "targets": [
            {"id": t.id, "name": t.name, "type": t.type.value,
             "accepts_files": sender.supports_files(t.chat_id)}
            for t in targets
        ],
    }


def research_content_type(output: OutputType):
    """Map the Composer's output type onto the legacy content_type column."""
    from ..models import ContentType

    return {
        OutputType.message: ContentType.news,
        OutputType.poll: ContentType.poll,
        OutputType.pdf: ContentType.resource,
        OutputType.excel: ContentType.resource,
    }[output]
