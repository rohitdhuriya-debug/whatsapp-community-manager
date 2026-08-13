"""Trending news: standing feeds, refreshed hourly, readable on demand."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, delete, select

from ..db import get_session
from ..models import Engine, NewsFeed, NewsItem
from ..services import news
from ..util import fmt_local, relative

router = APIRouter(prefix="/api/news", tags=["news"])


class FeedIn(BaseModel):
    name: str
    query: str
    category: str = ""
    active: bool = True


class FeedPatch(BaseModel):
    name: str | None = None
    query: str | None = None
    category: str | None = None
    active: bool | None = None


class AnalyseIn(BaseModel):
    engine: Engine = Engine.openrouter
    model_override: str | None = None


def _feed_dict(feed: NewsFeed, count: int) -> dict[str, Any]:
    return {
        "id": feed.id,
        "name": feed.name,
        "query": feed.query,
        "category": feed.category,
        "active": feed.active,
        "items": count,
        "last_fetched_at": feed.last_fetched_at.isoformat() if feed.last_fetched_at else None,
        "last_fetched_label": relative(feed.last_fetched_at) if feed.last_fetched_at else None,
    }


@router.get("/feeds")
def list_feeds(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    news.seed_defaults(session)
    feeds = session.exec(select(NewsFeed).order_by(NewsFeed.id)).all()
    out = []
    for feed in feeds:
        # The Search feed is storage for ad-hoc results, not a tab of its own.
        if feed.name == news.SEARCH_FEED_NAME:
            continue
        count = len(session.exec(select(NewsItem).where(NewsItem.feed_id == feed.id)).all())
        out.append(_feed_dict(feed, count))
    return out


@router.post("/feeds", status_code=201)
def create_feed(payload: FeedIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    if not payload.query.strip():
        raise HTTPException(status_code=400, detail="Give the feed something to search for.")
    feed = NewsFeed(
        name=payload.name.strip() or payload.query.strip()[:40],
        query=payload.query.strip(),
        category=payload.category.strip(),
        active=payload.active,
    )
    session.add(feed)
    session.commit()
    session.refresh(feed)
    return _feed_dict(feed, 0)


@router.patch("/feeds/{feed_id}")
def update_feed(
    feed_id: int, payload: FeedPatch, session: Session = Depends(get_session)
) -> dict[str, Any]:
    feed = session.get(NewsFeed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found.")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(feed, key, value)
    session.add(feed)
    session.commit()
    session.refresh(feed)
    count = len(session.exec(select(NewsItem).where(NewsItem.feed_id == feed_id)).all())
    return _feed_dict(feed, count)


@router.delete("/feeds/{feed_id}", status_code=204)
def delete_feed(feed_id: int, session: Session = Depends(get_session)) -> None:
    feed = session.get(NewsFeed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found.")
    # Items reference the feed, so they go first.
    session.exec(delete(NewsItem).where(NewsItem.feed_id == feed_id))
    session.exec(delete(NewsFeed).where(NewsFeed.id == feed_id))
    session.commit()


@router.get("/items")
def list_items(
    feed_id: int | None = None,
    limit: int = 60,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    news.seed_defaults(session)

    query = select(NewsItem)
    if feed_id is not None:
        query = query.where(NewsItem.feed_id == feed_id)
    else:
        # Search results are one-off lookups, not trending news.
        search_id = session.exec(
            select(NewsFeed.id).where(NewsFeed.name == news.SEARCH_FEED_NAME)
        ).first()
        if search_id is not None:
            query = query.where(NewsItem.feed_id != search_id)
    # Feeds overlap by design ("Indian stock market" and "Stocks in the news"
    # both catch a Sensex story), and dedupe at write time is per-feed so each
    # tab stays complete. Collapse across feeds only for the combined view, and
    # over-fetch first so the collapse cannot leave the page short.
    span = limit if feed_id is not None else limit * 3
    rows = session.exec(query.order_by(*news.recency_order()).limit(span)).all()

    feeds = {f.id: f for f in session.exec(select(NewsFeed)).all()}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if feed_id is None:
            key = row.dedupe_key or row.title.lower()
            if key in seen:
                continue
            seen.add(key)
        data = news.as_dict(row)
        feed = feeds.get(row.feed_id)
        data["feed_name"] = feed.name if feed else ""
        data["category"] = feed.category if feed else ""
        items.append(data)
        if len(items) >= limit:
            break

    latest = max((f.last_fetched_at for f in feeds.values() if f.last_fetched_at), default=None)
    return {
        "items": items,
        "count": len(items),
        "last_refresh": latest.isoformat() if latest else None,
        "last_refresh_label": fmt_local(latest) if latest else "never",
    }


@router.get("/search")
async def search_news(
    q: str, limit: int = 25, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Search the live news wire for anything, not just the standing feeds.

    Results are saved under an inactive "Search" feed, so each one carries a
    real id and the existing "What does this mean?" / "Post about this"
    buttons keep working unchanged.
    """
    text = " ".join((q or "").split())
    if not text:
        raise HTTPException(status_code=400, detail="Type something to search for.")

    try:
        rows = await news.search(session, text, limit=max(5, min(limit, 40)))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Search failed: {exc}") from exc

    items = []
    for row in rows:
        data = news.as_dict(row)
        data["feed_name"] = "Search"
        data["category"] = "Search"
        items.append(data)
    return {
        "items": items,
        "count": len(items),
        "query": text,
        "last_refresh_label": "just now",
    }


@router.post("/refresh")
async def refresh(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Pull every active feed now. Keyless, so this costs nothing."""
    news.seed_defaults(session)
    return await news.refresh_all(session)


@router.post("/items/{item_id}/analyse")
async def analyse_item(
    item_id: int, payload: AnalyseIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """What this headline means for India and its markets. One model call."""
    item = session.get(NewsItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Headline not found.")
    try:
        impact = await news.analyse(session, item, payload.engine, payload.model_override)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": item.id, "impact": impact}


@router.get("/items/{item_id}/brief")
def brief(item_id: int, session: Session = Depends(get_session)) -> dict[str, str]:
    """The headline turned into a Composer brief."""
    item = session.get(NewsItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Headline not found.")
    return {"brief": news.brief_for_composer(item), "title": item.title}
