"""Small shared helpers: time handling and text hygiene."""

from __future__ import annotations

from datetime import datetime, timezone

from .config import config

# ---------------------------------------------------------------------------
# Time
#
# SQLite/SQLAlchemy round-trips naive datetimes, so everything is stored as
# naive UTC and converted to Asia/Kolkata only for display (HC-5).
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Naive UTC timestamp, matching how datetimes are stored."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt: datetime | None) -> datetime | None:
    """Naive-UTC -> aware Asia/Kolkata."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(config.tz)


def fmt_local(dt: datetime | None, pattern: str = "%d %b %Y, %I:%M %p") -> str:
    """Human-readable IST string for templates. Empty dash when unset."""
    local = to_local(dt)
    return local.strftime(pattern) if local else "—"


def relative(dt: datetime | None) -> str:
    """Coarse 'time ago' / 'in ...' label. Good enough for a dashboard."""
    if dt is None:
        return "—"
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    delta = (dt - utcnow()).total_seconds()
    future = delta > 0
    secs = abs(delta)

    if secs < 60:
        text = "just now" if not future else "in under a minute"
        return text
    for limit, div, unit in ((3600, 60, "min"), (86400, 3600, "hour"), (2592000, 86400, "day")):
        if secs < limit:
            n = int(secs // div)
            plural = "" if n == 1 else "s"
            return f"in {n} {unit}{plural}" if future else f"{n} {unit}{plural} ago"
    n = int(secs // 2592000)
    plural = "" if n == 1 else "s"
    return f"in {n} month{plural}" if future else f"{n} month{plural} ago"


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def truncate(text: str | None, limit: int = 120) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
