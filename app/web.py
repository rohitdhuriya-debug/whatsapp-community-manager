"""Jinja2 setup and a small page-render helper.

Every HTML page needs the sidebar's target list, so that is injected here
rather than repeated in each route.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.responses import HTMLResponse

from .config import BASE_DIR, config
from .models import Draft, DraftStatus, Target
from .util import fmt_local, relative, truncate

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.filters["fmt_local"] = fmt_local
templates.env.filters["relative"] = relative
templates.env.filters["truncate_text"] = truncate


def page(
    request: Request,
    template: str,
    session: Session,
    **context: Any,
) -> HTMLResponse:
    targets = session.exec(select(Target).order_by(Target.name)).all()
    pending_count = len(
        session.exec(
            select(Draft).where(Draft.status == DraftStatus.pending)
        ).all()
    )
    base = {
        "request": request,
        "sidebar_targets": targets,
        "pending_count": pending_count,
        "timezone": config.timezone,
        "app_title": "WhatsApp Manager",
    }
    base.update(context)
    return templates.TemplateResponse(request, template, base)
