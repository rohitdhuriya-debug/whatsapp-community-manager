"""FastAPI entrypoint.

Run with:  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
or just:   ./start_all.command
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, config
from .db import init_db, session_scope
from .routers import (
    assets_api,
    autopilot_api,
    campaigns_api,
    devices_api,
    drafts_api,
    drive_api,
    logs_api,
    pages,
    schedules_api,
    settings_api,
    targets_api,
    waha_api,
)
from .security import require_token
from .services import llm, scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("Database ready at %s", config.db_path)
    scheduler.start()
    log.info("Dashboard: http://%s:%s", config.app_host, config.app_port)
    with session_scope() as session:
        if not llm.resolve_api_key(session):
            log.warning(
                "No OpenRouter API key yet - research works, drafting will not. "
                "Add one on the Settings page at http://%s:%s/settings",
                config.app_host,
                config.app_port,
            )
    yield
    scheduler.shutdown()
    log.info("Shutting down.")


app = FastAPI(
    title="WhatsApp Community Manager",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

# HTML pages are unauthenticated (localhost, single user - HC-6).
app.include_router(pages.router)

# /api/* honours the optional bearer token when APP_BEARER_TOKEN is set.
_guard = [Depends(require_token)]
app.include_router(waha_api.router, dependencies=_guard)
app.include_router(settings_api.router, dependencies=_guard)
app.include_router(targets_api.router, dependencies=_guard)
app.include_router(devices_api.router, dependencies=_guard)
app.include_router(campaigns_api.router, dependencies=_guard)
app.include_router(assets_api.router, dependencies=_guard)
app.include_router(autopilot_api.router, dependencies=_guard)
# Google redirects the browser to the OAuth callback, so it carries no bearer
# token and must stay outside the guard.
app.include_router(drive_api.router)
app.include_router(schedules_api.router, dependencies=_guard)
app.include_router(drafts_api.router, dependencies=_guard)
app.include_router(logs_api.router, dependencies=_guard)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """A dead WAHA or a flaky model must never take the dashboard down."""
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}"},
    )


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
