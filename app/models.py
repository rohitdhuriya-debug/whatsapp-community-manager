"""SQLModel tables backing app.db."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import Column, Text
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel

from .util import utcnow


class Platform(str, Enum):
    whatsapp = "whatsapp"
    telegram = "telegram"


class TargetType(str, Enum):
    group = "group"       # community announcement group -> chat_id ends @g.us
    channel = "channel"   # WhatsApp channel            -> chat_id ends @newsletter
    # Telegram equivalents. A supergroup is what a "community" becomes once it
    # grows, and Telegram treats channels separately just as WhatsApp does.
    supergroup = "supergroup"


class Language(str, Enum):
    hinglish = "hinglish"
    english = "english"


class ContentType(str, Enum):
    news = "news"
    tip = "tip"
    poll = "poll"
    resource = "resource"


class Engine(str, Enum):
    """Which brain writes the content."""

    openrouter = "openrouter"    # free :free models, fast, ~50/day
    claude_code = "claude_code"  # local Claude Code CLI on the Max plan


class OutputType(str, Enum):
    message = "message"  # plain WhatsApp text
    poll = "poll"
    pdf = "pdf"          # generated document, sent as a file
    excel = "excel"      # generated spreadsheet, sent as a file


class ScheduleKind(str, Enum):
    cron = "cron"          # recurring, 5-field expression
    once = "once"          # a single date + time
    # Every N days at a fixed time. Cron cannot express this: `*/2` on
    # day-of-month resets at month boundaries, so it is not alternate days.
    interval = "interval"


class DraftStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    sent = "sent"
    failed = "failed"


class Device(SQLModel, table=True):
    """One linked WhatsApp account = one WAHA session.

    WAHA 2026.7.x runs several sessions per container, so multiple phones can
    be linked without paying for WAHA Plus.
    """

    __tablename__ = "devices"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)                      # "My WhatsApp", "Work phone"
    platform: Platform = Field(default=Platform.whatsapp, index=True)

    # WhatsApp: the WAHA session id. Telegram: the bot's @username, kept here
    # so the column stays unique and human-readable across both.
    session_name: str = Field(index=True, unique=True)
    phone: str = ""                                     # filled in once paired
    push_name: str = ""                                 # profile / bot display name

    # Telegram only. Stored in the git-ignored app.db, never in the repo.
    bot_token: str = ""

    is_primary: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class Target(SQLModel, table=True):
    """One community or channel I admin, with its own persona and research brief."""

    __tablename__ = "targets"

    id: int | None = Field(default=None, primary_key=True)
    device_id: int | None = Field(default=None, foreign_key="devices.id", index=True)
    name: str = Field(index=True)
    niche: str = ""
    type: TargetType = Field(default=TargetType.group)
    # HC-7: only chat_ids stored here are ever sent to.
    chat_id: str = Field(index=True, unique=True)

    persona_prompt: str = Field(default="", sa_column=Column(Text))
    research_instructions: str = Field(default="", sa_column=Column(Text))
    tone: str = ""
    language: Language = Field(default=Language.hinglish)

    # Few-shot examples of my own best past messages.
    example_messages: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    banned_topics: str = Field(default="", sa_column=Column(Text))
    cta_link: str = ""
    model_override: str | None = Field(default=None)  # must end ":free"

    # "auto" infers from the niche/persona; "always"/"never" force it. Controls
    # the "Educational purpose only, not investment advice." line.
    disclaimer_mode: str = Field(default="auto")

    approval_required: bool = True
    # Where THIS chat's drafts get signed off: "dashboard" in the browser, or
    # "whatsapp" by replying to a message on your own number. Per-target, so a
    # channel can need a phone sign-off while a test group does not.
    approval_mode: str = Field(default="dashboard")
    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class Campaign(SQLModel, table=True):
    """A one-line brief broadcast to many chats at once.

    This is the Composer flow. Per-target personas still exist for the
    hands-off recurring drafts; a campaign is the deliberate, ad-hoc send.
    """

    __tablename__ = "campaigns"

    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    brief: str = Field(default="", sa_column=Column(Text))  # the one-liner
    language: str = Field(default="hinglish")               # free-form: any language
    engine: Engine = Field(default=Engine.openrouter)
    output_type: OutputType = Field(default=OutputType.message)
    model_override: str | None = None
    extra_instructions: str = Field(default="", sa_column=Column(Text))
    use_research: bool = True
    # Post page one of a PDF as an image before the document, so the feed shows
    # a preview rather than a bare filename.
    send_cover_image: bool = True
    # "per_target" (default) lets each target decide - see Target.approval_mode.
    # "dashboard" and "whatsapp" force one mode for the whole campaign.
    approval_mode: str = Field(default="per_target")
    # Channels render a document attachment poorly and WhatsApp scrapes any bare
    # link into an ugly card, so a channel post leads with a generated cover.
    generate_cover: bool = True
    created_at: datetime = Field(default_factory=utcnow, index=True)
    last_run_at: datetime | None = None


class Autopilot(SQLModel, table=True):
    """A standing brief that decides for itself what to post, every run.

    Set it up once with the community's context; on each firing it looks at
    what it has already sent, picks a fresh angle and a format, and hands that
    to the normal Composer pipeline via its backing campaign.
    """

    __tablename__ = "autopilots"

    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    # The backing campaign holds targets, language, engine and delivery
    # settings, so autopilot runs reuse the whole existing pipeline.
    campaign_id: int = Field(foreign_key="campaigns.id", index=True)

    # The one-line setup: who this community is and what it cares about.
    context: str = Field(default="", sa_column=Column(Text))
    avoid: str = Field(default="", sa_column=Column(Text))

    # "auto" lets the planner rotate formats; otherwise it is pinned.
    format_mode: str = Field(default="auto")
    formats: list[str] = Field(
        default_factory=lambda: ["message", "pdf", "excel", "poll"],
        sa_column=Column(JSON),
    )

    # Topics already used, newest first. Fed back to the planner so it never
    # repeats itself, and checked in code afterwards.
    recent_topics: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    recent_formats: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    approval_required: bool = True
    # Autopilot signs off separately from any single target, because it fires
    # unattended - "whatsapp" is the useful setting here.
    approval_mode: str = Field(default="dashboard")
    # Headlines already posted about, so a run never repeats a story even when
    # the wording differs enough to slip past the similarity check.
    recent_news_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON))
    active: bool = True
    last_plan: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
    last_run_at: datetime | None = None


class CampaignTarget(SQLModel, table=True):
    """Which chats a campaign goes to. HC-7 still applies: targets only."""

    __tablename__ = "campaign_targets"

    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaigns.id", index=True)
    target_id: int = Field(foreign_key="targets.id", index=True)


class Schedule(SQLModel, table=True):
    __tablename__ = "schedules"

    id: int | None = Field(default=None, primary_key=True)
    # Exactly one of target_id / campaign_id is set.
    target_id: int | None = Field(default=None, foreign_key="targets.id", index=True)
    campaign_id: int | None = Field(default=None, foreign_key="campaigns.id", index=True)

    kind: ScheduleKind = Field(default=ScheduleKind.cron)
    cron_expr: str = "0 9 * * *"       # m h dom mon dow, in Asia/Kolkata
    run_at: datetime | None = None     # naive UTC; start time for kind=interval
    interval_days: int | None = None   # for kind=interval
    content_type: ContentType = Field(default=ContentType.news)
    active: bool = True


class Draft(SQLModel, table=True):
    __tablename__ = "drafts"

    id: int | None = Field(default=None, primary_key=True)
    target_id: int = Field(foreign_key="targets.id", index=True)
    campaign_id: int | None = Field(default=None, foreign_key="campaigns.id", index=True)
    content_type: ContentType = Field(default=ContentType.news)

    # The research items the model was given, kept for auditability.
    research_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    content: str = Field(default="", sa_column=Column(Text))
    poll_options: list[str] | None = Field(default=None, sa_column=Column(JSON))

    # Generated PDF / Excel, when the campaign produces a file. `content`
    # doubles as the accompanying WhatsApp caption.
    output_type: OutputType = Field(default=OutputType.message)
    asset_path: str | None = None      # on-disk path under ./assets
    asset_filename: str | None = None  # what the recipient sees
    asset_mime: str | None = None

    # Channel posts lead with a picture and link to the file rather than
    # attaching it, because channels do not render document attachments well.
    image_path: str | None = None
    image_mime: str | None = None
    drive_link: str | None = None
    engine_used: str | None = None
    model_used: str | None = None

    status: DraftStatus = Field(default=DraftStatus.pending, index=True)
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, index=True)
    sent_at: datetime | None = None


class SendLog(SQLModel, table=True):
    """HC-7: every outbound attempt is logged, success or failure."""

    __tablename__ = "send_log"

    id: int | None = Field(default=None, primary_key=True)
    target_id: int = Field(foreign_key="targets.id", index=True)
    draft_id: int | None = Field(default=None, foreign_key="drafts.id", index=True)
    chat_id: str = ""
    status: str = ""  # "sent" | "failed"
    response_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, index=True)


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class ApprovalRequest(SQLModel, table=True):
    """An approval waiting on a WhatsApp reply.

    The draft is sent to your own number; replying /approve there releases it
    to the communities. Lets you sign off from the phone without opening the
    dashboard.
    """

    __tablename__ = "approval_requests"

    id: int | None = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaigns.id", index=True)
    # Which chat this request covers. None means the whole campaign (the old
    # behaviour). Set per-target so approving one channel cannot release the
    # drafts of a target that was set to sign off in the dashboard.
    target_id: int | None = Field(default=None, foreign_key="targets.id", index=True)

    # Short human-typable code, so several pending approvals stay unambiguous.
    code: str = Field(index=True)
    chat_id: str = ""          # where the request was sent
    session_name: str = ""     # which WAHA session owns that chat

    status: ApprovalStatus = Field(default=ApprovalStatus.pending, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    resolved_at: datetime | None = None
    # WhatsApp timestamps are unix seconds; only replies after this count.
    sent_ts: int = 0
    note: str = ""


class NewsFeed(SQLModel, table=True):
    """A standing news query, refreshed on a schedule.

    Built for the "what happened, and what does it mean for India" workflow:
    each feed is a slice of the world you want watched.
    """

    __tablename__ = "news_feeds"

    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    query: str = Field(default="", sa_column=Column(Text))
    # Free-form label shown as a chip: "Geopolitics", "Indian markets", …
    category: str = ""
    region: str = "in-en"
    active: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    last_fetched_at: datetime | None = None


class NewsItem(SQLModel, table=True):
    """One headline. Deduped per feed by a normalised title."""

    __tablename__ = "news_items"

    id: int | None = Field(default=None, primary_key=True)
    feed_id: int = Field(foreign_key="news_feeds.id", index=True)
    title: str = Field(default="", sa_column=Column(Text))
    dedupe_key: str = Field(default="", index=True)
    url: str = Field(default="", sa_column=Column(Text))
    source: str = ""
    snippet: str = Field(default="", sa_column=Column(Text))
    published: str = ""
    # When the article was published, not when we happened to find it. Sorting
    # and the freshness cutoff both use this; `fetched_at` only says when we
    # last looked. None when the source gave no parseable date.
    published_at: datetime | None = Field(default=None, index=True)
    via: str = ""
    # Filled in on demand - what this means for India and its markets.
    impact: str | None = Field(default=None, sa_column=Column(Text))
    fetched_at: datetime = Field(default_factory=utcnow, index=True)


class Setting(SQLModel, table=True):
    """Simple key-value store for app-wide settings."""

    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: str = ""
