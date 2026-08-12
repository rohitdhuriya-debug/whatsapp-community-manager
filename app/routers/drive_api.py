"""Google Drive connection, driven entirely from the Settings page."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlmodel import Session

from ..db import get_session
from ..services import drive

router = APIRouter(prefix="/api/drive", tags=["drive"])


class ClientConfig(BaseModel):
    client_json: str


class ProviderChoice(BaseModel):
    provider: str  # "mcp" | "api" | "off"


@router.get("/status")
async def status(session: Session = Depends(get_session)) -> dict[str, Any]:
    info = await drive.combined_status(session)
    if info["api"]["connected"]:
        info["api"]["email"] = drive.account_email(session)
    return info


@router.patch("/provider")
def set_provider(
    payload: ProviderChoice, session: Session = Depends(get_session)
) -> dict[str, str]:
    try:
        drive.set_provider(session, payload.provider)
    except drive.DriveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"provider": drive.get_provider(session)}


@router.post("/credentials")
def save_credentials(
    payload: ClientConfig, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        drive.save_client_config(session, payload.client_json)
    except drive.DriveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return drive.status(session)


@router.get("/auth-url")
def get_auth_url(session: Session = Depends(get_session)) -> dict[str, str]:
    try:
        return {"url": drive.auth_url(session)}
    except drive.DriveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/oauth/callback", include_in_schema=False)
def oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Where Google sends the browser back after consent."""
    if error:
        return _page("Drive connection cancelled", error, ok=False)
    if not code:
        return _page("Missing authorisation code", "Google did not return a code.", ok=False)

    try:
        drive.exchange_code(session, code, state=state)
    except drive.DriveError as exc:
        return _page("Could not connect Drive", str(exc), ok=False)

    return _page("Google Drive connected", "You can close this tab and go back.", ok=True)


def _page(title: str, message: str, *, ok: bool) -> HTMLResponse:
    colour = "#16A34A" if ok else "#DC2626"
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8">
<meta name="color-scheme" content="light">
<title>{title}</title>
<body style="margin:0;background:#fff;color:#111827;
  font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  display:grid;place-items:center;min-height:100vh">
<div style="max-width:420px;text-align:center;padding:32px;border:1px solid #E5E7EB;
     border-radius:16px">
  <div style="font-size:19px;font-weight:600;color:{colour}">{title}</div>
  <p style="color:#6B7280;margin:10px 0 22px">{message}</p>
  <a href="/settings" style="display:inline-block;background:#4F46E5;color:#fff;
     text-decoration:none;padding:10px 18px;border-radius:12px;font-weight:500">
    Back to Settings</a>
</div></body>"""
    )


@router.post("/disconnect")
def disconnect(session: Session = Depends(get_session)) -> dict[str, Any]:
    drive.disconnect(session)
    return drive.status(session)


@router.post("/test")
async def test_connection(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Prove the selected provider actually works, end to end."""
    provider = drive.get_provider(session)

    if provider == drive.PROVIDER_OFF:
        raise HTTPException(status_code=400, detail="Drive uploads are turned off.")

    if provider == drive.PROVIDER_MCP:
        from ..services import drive_mcp

        check = await drive_mcp.check()
        if not check["available"]:
            raise HTTPException(
                status_code=400,
                detail=f"{check['detail']} {check.get('hint', '')}".strip(),
            )
        return {"ok": True, "provider": "mcp", "folder": drive.FOLDER_NAME,
                "email": "", "files": [], "detail": check["detail"]}

    try:
        service = drive._service(session)
        folder = drive._folder_id(session, service)
        files = service.files().list(
            q=f"'{folder}' in parents and trashed=false",
            fields="files(id,name)", pageSize=5,
        ).execute().get("files", [])
    except drive.DriveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Drive error: {exc}") from exc

    return {
        "ok": True,
        "provider": "api",
        "email": drive.account_email(session),
        "folder": drive.FOLDER_NAME,
        "files": [f["name"] for f in files],
        "detail": "Connected via your own Google OAuth client.",
    }
