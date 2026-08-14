"""Keyless research (FR-1).

Two free, unlimited sources:
  * DuckDuckGo via the `ddgs` package
  * Google News RSS via feedparser

No API keys, no quotas, so a message costs exactly one OpenRouter call and
nothing else.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import urllib.parse
from datetime import datetime
from typing import Any

import feedparser
from ddgs import DDGS

log = logging.getLogger(__name__)

# Seconds. feedparser blocks with no timeout of its own.
FEED_TIMEOUT = 20

# The default ddgs backend rotation includes engines that hang or return
# nothing from India; these two are the ones that actually answer.
BACKENDS = ("duckduckgo", "brave")

DDG_MAX_RESULTS = 8
GNEWS_MAX_RESULTS = 8
RETURN_TOP = 6
MAX_PER_SOURCE = 2  # keeps one outlet from dominating the brief

RECENCY_HINTS = {
    "news": "this week latest",
    "tip": "practical guide 2026",
    "resource": "best tools resources",
    "poll": "trending debate opinion",
}


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)  # RSS summaries are HTML
    return " ".join(html.unescape(text).split())


def _domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def _title_key(title: str) -> str:
    """Loose key so 'OpenAI launches X' and 'OpenAI Launches X!' collapse."""
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()[:70]


def _build_query(instructions: str, content_type: str) -> str:
    base = " ".join((instructions or "").split())
    if not base:
        base = "trending news"
    hint = RECENCY_HINTS.get(content_type, "latest")
    # Long briefs are prose, not queries - keep the query short enough to match.
    if len(base) > 180:
        base = base[:180].rsplit(" ", 1)[0]
    return f"{base} {hint}"


# ---------------------------------------------------------------------------
# Sources (both blocking - called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _search_ddg(query: str) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for backend in BACKENDS:
        try:
            rows = DDGS(timeout=20).text(
                query, region="in-en", max_results=DDG_MAX_RESULTS, backend=backend
            )
        except Exception as exc:  # ddgs raises several bespoke types
            last_error = exc
            log.warning("ddgs backend '%s' failed: %s", backend, exc)
            continue

        items = []
        for row in rows or []:
            url = row.get("href") or ""
            items.append(
                {
                    "title": _clean(row.get("title")),
                    "snippet": _clean(row.get("body"))[:400],
                    "url": url,
                    "published": "",
                    "source": _domain(url),
                    "via": backend,
                }
            )
        if items:
            return items

    if last_error:
        log.warning("All ddgs backends failed; continuing with news only.")
    return []


def _search_gnews(query: str) -> list[dict[str, Any]]:
    url = (
        "https://news.google.com/rss/search?q="
        f"{urllib.parse.quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    )
    # feedparser has no timeout of its own, so a stalled feed would hang the
    # caller indefinitely.
    import socket

    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(FEED_TIMEOUT)
    try:
        feed = feedparser.parse(url)
    except Exception as exc:
        log.warning("Google News RSS failed: %s", exc)
        return []
    finally:
        socket.setdefaulttimeout(previous)

    items = []
    for entry in (feed.entries or [])[:GNEWS_MAX_RESULTS]:
        title = _clean(entry.get("title"))
        source = _clean((entry.get("source") or {}).get("title")) if entry.get("source") else ""
        # Google News titles end with " - Outlet"; drop it, we track source separately.
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)]
        items.append(
            {
                "title": title,
                "snippet": "",
                "url": entry.get("link", ""),
                "published": _clean(entry.get("published")),
                "source": source or "news.google.com",
                "via": "google-news",
            }
        )
    return items


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def _merge(ddg: list[dict[str, Any]], news: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Interleave both feeds, drop duplicate headlines, cap per outlet."""
    merged: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    per_source: dict[str, int] = {}

    for pair in zip(news, ddg):  # news first - it carries timestamps
        for item in pair:
            _consider(item, merged, seen_titles, per_source)
    for item in news[len(ddg):] + ddg[len(news):]:
        _consider(item, merged, seen_titles, per_source)

    return merged[:RETURN_TOP]


def _consider(
    item: dict[str, Any],
    merged: list[dict[str, Any]],
    seen_titles: set[str],
    per_source: dict[str, int],
) -> None:
    title = item.get("title", "")
    if not title or not item.get("url"):
        return
    key = _title_key(title)
    if not key or key in seen_titles:
        return
    source = item.get("source") or "unknown"
    if per_source.get(source, 0) >= MAX_PER_SOURCE:
        return
    seen_titles.add(key)
    per_source[source] = per_source.get(source, 0) + 1
    merged.append(item)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def research(instructions: str, content_type: str = "news") -> list[dict[str, Any]]:
    """Return up to 6 {title, snippet, url, published, source} items."""
    query = _build_query(instructions, content_type)
    log.info("Researching: %s", query)

    ddg, news = await asyncio.gather(
        asyncio.to_thread(_search_ddg, query),
        asyncio.to_thread(_search_gnews, query),
        return_exceptions=True,
    )
    if isinstance(ddg, BaseException):
        log.warning("DuckDuckGo leg failed: %s", ddg)
        ddg = []
    if isinstance(news, BaseException):
        log.warning("Google News leg failed: %s", news)
        news = []

    items = _merge(ddg, news)
    log.info("Research returned %s item(s) for '%s'", len(items), query[:60])
    return items


def format_for_prompt(items: list[dict[str, Any]]) -> str:
    """Render research as a compact block for the model."""
    if not items:
        return "(No fresh sources found - write from general knowledge and stay evergreen.)"
    lines = []
    for i, item in enumerate(items, 1):
        line = f"{i}. {item['title']}"
        if item.get("source"):
            line += f"  [{item['source']}]"
        if item.get("published"):
            line += f"  ({item['published']})"
        if item.get("snippet"):
            line += f"\n   {item['snippet'][:280]}"
        lines.append(line)
    return "\n".join(lines)


def today_label() -> str:
    from ..config import config

    return datetime.now(config.tz).strftime("%A, %d %B %Y")
