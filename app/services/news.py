"""Standing news feeds, refreshed on a schedule.

Built around one question: something happened in the world - what does it mean
for India, and for Indian markets? Feeds are just saved queries, so the set is
yours to shape.

Fetching is keyless (Google News RSS), so refreshing costs nothing and can run
hourly. Only the optional impact read costs a model call, and only when you ask
for it.

Web search is deliberately not a fallback here. It answers these queries with
market-data homepages ("Share Market Live", "Stock Market Today | Moneycontrol")
that carry no publish date, so they both pollute the feed and defeat the
recency sort. An empty feed is better than a feed of homepages.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, delete, select

from ..models import NewsFeed, NewsItem
from ..util import utcnow
from . import engines, research

log = logging.getLogger(__name__)

# Headlines older than this are pruned so the tab stays about *now*.
RETAIN_HOURS = 72
PER_FEED_KEEP = 60
RSS_PER_FEED = 25

# This is a *trending* feed, so recency is the whole point. Google News sorts by
# relevance, which happily returns week-old pieces, so the window is asked for
# in the query (`when:`) and enforced again on the parsed date - the operator is
# undocumented and occasionally ignored.
FRESH_WINDOWS = ["when:1d", "when:2d", "when:7d"]
MAX_AGE_HOURS = 36
FRESH_HOURS = 6          # highlighted green in the feed
# If the tight window yields fewer than this, widen once rather than show a
# near-empty feed on a quiet news day.
MIN_ITEMS = 6

# What the app starts with. Chosen for the global-event -> India -> markets
# chain, which is the thing being watched.
#
# Queries are deliberately SHORT. Google News ANDs every term, so a wordy query
# matches nothing recent and the feed comes back empty - measured, not guessed:
# the original 13-word "stocks in the news" query returned 0 items where its
# first 6 words returned 25.
DEFAULT_FEEDS = [
    {
        "name": "Geopolitics → India impact",
        "category": "Geopolitics",
        "query": "geopolitical tensions India",
    },
    {
        "name": "Indian stock market",
        "category": "Indian markets",
        "query": "Nifty Sensex Indian stock market",
    },
    {
        "name": "Global macro & central banks",
        "category": "Global macro",
        "query": "Federal Reserve inflation crude oil global markets",
    },
    {
        "name": "Stocks in the news",
        "category": "Stocks",
        "query": "Indian stocks news NSE BSE",
    },
    {
        # Measured against alternatives: "AI news" returned 25 clean industry
        # headlines with zero stock-prediction spam, where "artificial
        # intelligence" was polluted with Motley Fool "this AI stock will
        # double" pieces.
        "name": "AI",
        "category": "AI",
        "query": "AI news",
    },
]

# Bumped whenever DEFAULT_FEEDS gains an entry, so an existing install picks it
# up. A plain "is the table empty" check would never seed a 5th feed.
DEFAULTS_VERSION = 2
DEFAULTS_SETTING = "news_defaults_version"

# Ad-hoc searches land in their own inactive feed: item ids stay real (so
# "What does this mean?" and "Post about this" work unchanged), _prune keeps it
# bounded, and the hourly refresh skips it because it is not active.
SEARCH_FEED_NAME = "Search"


def _dedupe_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()[:90]


# Web search answers "stock market today" with Moneycontrol's homepage, not with
# news. These patterns catch those landing pages so the feed stays events-only.
LANDING_PAGE = re.compile(
    r"(share|stock)\s+(market|price)\s+live"
    r"|live\s+(updates?|blog|news)$"
    r"|latest\s+news\s+on\b"
    r"|^\s*(nse|bse)\b.*\b(ltd|exchange)\b"
    r"|live\s+indices"
    r"|\bhome\s*page\b"
    r"|news\s+updates?$"
    # "Stock Market News Today: Sensex, Nifty, IPO, Market Analysis"
    r"|\b(stock|share)\s+market\s+news\s+today\b"
    r"|^\s*(top\s+|live\s+)?(stock|share)\s+market\b.*\|"
    # A section index, not a story: no verb, just a list of tickers/tags.
    r"|\bmarket\s+today\s*[:|-]"
    r"|^\s*live\s+(stock|share)\s+market\b"
    # "…, NSE/BSE Live Updates, Stock Market Today" - a rolling live blog
    # rather than a story, wherever the phrase sits in the title.
    r"|\blive\s+updates?\b"
    r"|\b(stock|share)\s+market\s+today\s*$",
    re.IGNORECASE,
)


def looks_like_landing_page(title: str) -> bool:
    if LANDING_PAGE.search(title or ""):
        return True
    # "Stock Market: Stock Market Today | Stock Market Live - Moneycontrol"
    # Real headlines rarely repeat the same phrase three times.
    words = re.findall(r"[a-z]+", (title or "").lower())
    if len(words) >= 6:
        common = max((words.count(w) for w in set(words)), default=0)
        if common >= 3:
            return True
    return False


def _entry_published(entry: Any) -> datetime | None:
    """RSS date -> naive UTC, matching how the rest of the app stores time."""
    import calendar

    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    try:
        # published_parsed is already UTC; timegm avoids the local-time shift
        # mktime would introduce.
        return datetime.utcfromtimestamp(calendar.timegm(struct))
    except Exception:
        return None


def _fetch_rss(query: str, limit: int, window: str = "") -> list[dict[str, Any]]:
    """Google News for this query, at feed depth rather than research depth."""
    import html as html_mod
    import urllib.parse

    import feedparser

    full = f"{query} {window}".strip() if window else query
    url = (
        "https://news.google.com/rss/search?q="
        f"{urllib.parse.quote(full)}&hl=en-IN&gl=IN&ceid=IN:en"
    )
    try:
        parsed = feedparser.parse(url)
    except Exception as exc:
        log.warning("News RSS failed for %r: %s", query[:50], exc)
        return []

    out: list[dict[str, Any]] = []
    for entry in (parsed.entries or [])[:limit]:
        title = html_mod.unescape(entry.get("title", "")).strip()
        source = ""
        if entry.get("source"):
            source = html_mod.unescape((entry.get("source") or {}).get("title", ""))
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)]
        out.append({
            "title": title,
            "snippet": "",
            "url": entry.get("link", ""),
            "source": source or "news.google.com",
            "published": entry.get("published", ""),
            "published_at": _entry_published(entry),
            "via": "google-news",
        })
    return out


def _recent(items: list[dict[str, Any]], max_age_hours: int) -> list[dict[str, Any]]:
    """Keep only what is actually recent, newest first.

    An item with no parseable date is kept - dropping it would silently lose
    real headlines from sources with sloppy feeds - but it sorts below every
    dated one so it can never head the feed.
    """
    cutoff = utcnow() - timedelta(hours=max_age_hours)
    kept = [
        item for item in items
        if item.get("published_at") is None or item["published_at"] >= cutoff
    ]
    kept.sort(key=lambda i: i.get("published_at") or datetime.min, reverse=True)
    return kept


def seed_defaults(session: Session) -> None:
    """Give the tab something to show, and add feeds shipped since last time.

    Versioned rather than "seed only when empty": this runs on every feed and
    item request, so it must be cheap, must not resurrect a feed the user
    deleted, and must still deliver a newly shipped default to an install that
    already has the earlier ones.
    """
    from ..db import get_setting, set_setting

    try:
        applied = int(get_setting(session, DEFAULTS_SETTING, "0") or 0)
    except ValueError:
        applied = 0

    existing = session.exec(select(NewsFeed)).all()
    if applied == 0 and existing:
        # Pre-versioning install: it already has v1, so only later ones apply.
        applied = 1
    if applied >= DEFAULTS_VERSION:
        return

    known = {(f.name or "").strip().lower() for f in existing}
    added = 0
    for spec in DEFAULT_FEEDS:
        if spec["name"].strip().lower() in known:
            continue
        session.add(NewsFeed(**spec))
        added += 1

    set_setting(session, DEFAULTS_SETTING, str(DEFAULTS_VERSION))
    session.commit()
    if added:
        log.info("Seeded %s news feed(s) at version %s.", added, DEFAULTS_VERSION)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


async def fetch_feed(session: Session, feed: NewsFeed) -> int:
    """Refresh one feed. Returns how many genuinely new items landed."""
    query = " ".join((feed.query or feed.name).split())

    # News RSS only. General web search answers these queries with market-data
    # homepages ("Share Market Live", "Stock Market Today | Moneycontrol"),
    # which is noise in a headline feed. It stays in research.py, where the
    # broader net is genuinely useful.
    #
    # Start at 24h and widen only if the day is genuinely quiet, so the feed
    # leads with today's news instead of whatever Google ranks highest.
    # Google News ANDs the terms, so a long query can match nothing at all -
    # which is what a wordy feed query looks like from here. Trimming to the
    # leading terms is what rescues it; widening the date window cannot.
    attempts = [query]
    if len(query.split()) > 6:
        attempts.append(" ".join(query.split()[:6]))

    news: list[dict[str, Any]] = []
    for text in attempts:
        for window in FRESH_WINDOWS:
            raw = await asyncio.to_thread(_fetch_rss, text, RSS_PER_FEED, window)
            found = _recent(
                raw, MAX_AGE_HOURS if window == "when:1d" else RETAIN_HOURS
            )
            if len(found) > len(news):
                news = found
            if len(news) >= MIN_ITEMS:
                break
        if len(news) >= MIN_ITEMS:
            break
        log.info("Feed %s: %r gave %s item(s); retrying shorter.", feed.id, text[:40], len(news))

    known = {
        row.dedupe_key
        for row in session.exec(
            select(NewsItem).where(NewsItem.feed_id == feed.id)
        ).all()
    }

    added = 0
    for item in news:
        title = (item.get("title") or "").strip()
        key = _dedupe_key(title)
        if not title or not key or key in known:
            continue
        if looks_like_landing_page(title):
            continue
        known.add(key)
        session.add(NewsItem(
            feed_id=feed.id,
            title=title,
            dedupe_key=key,
            url=item.get("url", ""),
            source=item.get("source", ""),
            snippet=(item.get("snippet") or "")[:600],
            published=item.get("published", ""),
            published_at=item.get("published_at"),
            via=item.get("via", ""),
            fetched_at=utcnow(),
        ))
        added += 1

    feed.last_fetched_at = utcnow()
    session.add(feed)
    session.commit()

    _prune(session, feed.id)
    log.info("Feed %s (%s): %s new item(s).", feed.id, feed.name, added)
    return added


def recency_order():
    """Newest-published first, with undated items last.

    Coalescing to `fetched_at` looks tidier but is wrong: an undated item is
    fetched *now*, so it would outrank every real headline and head the feed
    forever. Sort the undated ones to the bottom instead.
    """
    return (NewsItem.published_at.is_(None), NewsItem.published_at.desc(),
            NewsItem.fetched_at.desc())


def _prune(session: Session, feed_id: int) -> None:
    """Drop anything stale, and cap how much history a feed keeps."""
    from sqlalchemy import func

    age = func.coalesce(NewsItem.published_at, NewsItem.fetched_at)
    cutoff = utcnow() - timedelta(hours=RETAIN_HOURS)
    session.exec(delete(NewsItem).where(NewsItem.feed_id == feed_id, age < cutoff))

    rows = session.exec(
        select(NewsItem.id)
        .where(NewsItem.feed_id == feed_id)
        .order_by(*recency_order())
        .offset(PER_FEED_KEEP)
    ).all()
    if rows:
        session.exec(delete(NewsItem).where(NewsItem.id.in_(list(rows))))
    session.commit()


def search_feed(session: Session) -> NewsFeed:
    """The inactive feed ad-hoc searches are stored under."""
    feed = session.exec(
        select(NewsFeed).where(NewsFeed.name == SEARCH_FEED_NAME)
    ).first()
    if feed is None:
        feed = NewsFeed(
            name=SEARCH_FEED_NAME, category="Search", query="",
            # Inactive so refresh_all() never re-runs a one-off search.
            active=False,
        )
        session.add(feed)
        session.commit()
        session.refresh(feed)
    return feed


async def search(session: Session, query: str, *, limit: int = 25) -> list[NewsItem]:
    """Search the live news wire, newest first.

    Results are persisted under the Search feed rather than returned raw, so
    they carry real ids - "What does this mean?" and "Post about this" both
    look items up by id and would otherwise 404 on a search result.
    """
    text = " ".join((query or "").split())
    if not text:
        return []
    # Same lesson as the standing feeds: Google News ANDs every term.
    if len(text.split()) > 6:
        text = " ".join(text.split()[:6])

    feed = search_feed(session)
    found: list[dict[str, Any]] = []
    for window in FRESH_WINDOWS:
        raw = await asyncio.to_thread(_fetch_rss, text, limit, window)
        found = _recent(raw, MAX_AGE_HOURS if window == "when:1d" else RETAIN_HOURS)
        if len(found) >= MIN_ITEMS:
            break

    known = {
        row.dedupe_key: row
        for row in session.exec(select(NewsItem).where(NewsItem.feed_id == feed.id)).all()
    }
    out: list[NewsItem] = []
    for item in found:
        title = (item.get("title") or "").strip()
        key = _dedupe_key(title)
        if not title or not key or looks_like_landing_page(title):
            continue
        existing = known.get(key)
        if existing is not None:
            out.append(existing)
            continue
        row = NewsItem(
            feed_id=feed.id, title=title, dedupe_key=key,
            url=item.get("url", ""), source=item.get("source", ""),
            snippet=(item.get("snippet") or "")[:600],
            published=item.get("published", ""),
            published_at=item.get("published_at"),
            via=item.get("via", ""), fetched_at=utcnow(),
        )
        session.add(row)
        known[key] = row
        out.append(row)

    feed.query = text
    feed.last_fetched_at = utcnow()
    session.add(feed)
    session.commit()
    for row in out:
        session.refresh(row)

    _prune(session, feed.id)
    log.info("Search %r: %s result(s).", text, len(out))
    # _prune may have deleted the tail; only return what still exists. Sort
    # here rather than trusting fetch order - `out` mixes freshly fetched rows
    # with ones a previous search already stored.
    alive = [r for r in out if session.get(NewsItem, r.id) is not None]
    alive.sort(key=lambda r: r.published_at or datetime.min, reverse=True)
    return alive


def fresh_headlines(
    session: Session, *, hours: int = 24, limit: int = 12, widen_to: int = 48
) -> list[NewsItem]:
    """Recent, dated, deduped headlines from the standing feeds.

    Undated rows are excluded outright: this feeds Autopilot, and a headline
    with no publish date cannot be shown to be recent.
    """
    search_id = session.exec(
        select(NewsFeed.id).where(NewsFeed.name == SEARCH_FEED_NAME)
    ).first()

    def _query(window: int) -> list[NewsItem]:
        cutoff = utcnow() - timedelta(hours=window)
        stmt = select(NewsItem).where(
            NewsItem.published_at.is_not(None),
            NewsItem.published_at >= cutoff,
        )
        if search_id is not None:
            stmt = stmt.where(NewsItem.feed_id != search_id)
        return list(session.exec(stmt.order_by(NewsItem.published_at.desc()).limit(limit * 4)).all())

    rows = _query(hours)
    if len(rows) < 5:
        rows = _query(widen_to)

    seen: set[str] = set()
    out: list[NewsItem] = []
    for row in rows:
        key = row.dedupe_key or row.title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def format_for_planner(items: list[NewsItem]) -> str:
    """Numbered headlines with their age, for the planner prompt."""
    from ..util import relative

    lines = []
    for item in items:
        when = relative(item.published_at or item.fetched_at)
        lines.append(f"[{item.id}] {item.title} ({item.source or 'news'}, {when})")
    return "\n".join(lines)


async def refresh_all(session: Session) -> dict[str, Any]:
    """Refresh every active feed. Safe to run hourly - no API keys, no quota."""
    feeds = session.exec(select(NewsFeed).where(NewsFeed.active)).all()
    total = 0
    per_feed: list[dict[str, Any]] = []
    for feed in feeds:
        try:
            count = await fetch_feed(session, feed)
        except Exception as exc:
            log.warning("Feed %s failed: %s", feed.id, exc)
            per_feed.append({"id": feed.id, "name": feed.name, "error": str(exc)[:120]})
            continue
        total += count
        per_feed.append({"id": feed.id, "name": feed.name, "added": count})
    return {"feeds": len(feeds), "added": total, "detail": per_feed}


# ---------------------------------------------------------------------------
# Impact read - the "what does this mean for India" layer
# ---------------------------------------------------------------------------


IMPACT_SYSTEM = """You are a markets analyst writing for Indian investors.

Given a headline, explain what it actually means. Be concrete and short.

Cover, in this order and only where genuinely relevant:
- What happened, in one line
- Global impact
- Impact on India specifically
- Likely effect on Indian equities - name sectors or stock types, not tickers
  you cannot verify

Rules:
- 90 words maximum, total.
- No preamble, no headings, no bullet characters. Plain sentences.
- If the link between the event and Indian markets is weak, say so plainly
  rather than inventing one.
- Never give buy or sell advice, and never predict price levels."""


async def analyse(session: Session, item: NewsItem, engine, model: str | None = None) -> str:
    """One model call: what this headline means for India and its markets."""
    messages = [
        {"role": "system", "content": IMPACT_SYSTEM},
        {"role": "user", "content":
            f"Headline: {item.title}\n"
            f"Source: {item.source}\n"
            f"{'Summary: ' + item.snippet if item.snippet else ''}\n\n"
            "What does this mean?"},
    ]
    text, _label = await engines.generate(session, engine, messages, model_override=model)
    item.impact = text.strip()
    session.add(item)
    session.commit()
    return item.impact


def brief_for_composer(item: NewsItem) -> str:
    """Turn a headline into a Composer brief."""
    parts = [f"Write about this for the community: {item.title}"]
    if item.impact:
        parts.append(f"Context and impact: {item.impact}")
    elif item.snippet:
        parts.append(f"Context: {item.snippet}")
    if item.source:
        parts.append(f"Source: {item.source}")
    parts.append(
        "Explain what it means for them specifically. Educational only, "
        "no buy or sell calls."
    )
    return "\n".join(parts)


def today_label() -> str:
    return research.today_label()


def as_dict(item: NewsItem) -> dict[str, Any]:
    from ..util import relative

    return {
        "id": item.id,
        "feed_id": item.feed_id,
        "title": item.title,
        "url": item.url,
        "source": item.source,
        "snippet": item.snippet,
        "published": item.published,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        # What the card shows: how old the *story* is, not how long ago we polled.
        "age_label": relative(item.published_at or item.fetched_at),
        "dated": item.published_at is not None,
        # Highlighted in the feed: broke within the last few hours.
        "fresh": bool(
            item.published_at
            and (utcnow() - item.published_at) < timedelta(hours=FRESH_HOURS)
        ),
        "via": item.via,
        "impact": item.impact,
        "fetched_at": item.fetched_at.isoformat(),
        "fetched_label": relative(item.fetched_at),
    }
