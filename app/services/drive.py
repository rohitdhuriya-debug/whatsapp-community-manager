"""Google Drive: host generated assets and hand back a shareable link.

WhatsApp channels do not reliably render document attachments, so a channel
post carries an image plus a link instead of the file itself. Drive is where
that link points.

Auth is a normal OAuth 2.0 web flow against a client you create yourself, so
nothing here costs money and no credentials are ever bundled with the app.
Scope is `drive.file`: this app can only ever see files it created, never the
rest of your Drive.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from sqlmodel import Session

from ..config import config
from ..db import get_setting, set_setting

log = logging.getLogger(__name__)

# Only files this app creates. Never the user's existing Drive contents.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

CLIENT_KEY = "drive_client_config"
TOKEN_KEY = "drive_token"
FOLDER_KEY = "drive_folder_id"
# PKCE verifier and CSRF state, held between the auth redirect and the callback.
VERIFIER_KEY = "drive_code_verifier"
STATE_KEY = "drive_oauth_state"

FOLDER_NAME = "WhatsApp Manager"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveError(RuntimeError):
    """Any Drive failure. Message is safe to show in the UI."""


def redirect_uri() -> str:
    """Must match a redirect URI on the OAuth client exactly.

    Follows PUBLIC_URL when the app is behind a tunnel: Google redirects the
    browser here, and a localhost address only resolves on the machine running
    the app - useless when connecting Drive from a phone.
    """
    return f"{config.base_url}/api/drive/oauth/callback"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def save_client_config(session: Session, raw: str) -> None:
    """Store the OAuth client JSON downloaded from Google Cloud Console."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DriveError(f"That is not valid JSON: {exc}") from exc

    root = data.get("web") or data.get("installed")
    if not root or not root.get("client_id") or not root.get("client_secret"):
        raise DriveError(
            "This JSON has no client_id/client_secret. Download the OAuth client "
            "credentials from Google Cloud Console (APIs & Services → Credentials)."
        )
    # Normalise to the "web" shape the flow helper expects.
    normalised = {"web": {**root, "redirect_uris": [redirect_uri()]}}
    set_setting(session, CLIENT_KEY, json.dumps(normalised))


def client_config(session: Session) -> dict[str, Any] | None:
    raw = get_setting(session, CLIENT_KEY, "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def is_configured(session: Session) -> bool:
    return client_config(session) is not None


def is_connected(session: Session) -> bool:
    return bool(get_setting(session, TOKEN_KEY, ""))


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def _flow(
    session: Session, *, state: str | None = None, code_verifier: str | None = None
) -> Flow:
    cfg = client_config(session)
    if cfg is None:
        raise DriveError("Add your Google OAuth client JSON first.")

    flow = Flow.from_client_config(
        cfg,
        scopes=SCOPES,
        redirect_uri=redirect_uri(),
        state=state,
        # Never let the library invent a verifier on its own: the auth request
        # and the token exchange happen in two different requests, so the value
        # has to be one we control and can persist between them.
        autogenerate_code_verifier=False,
    )
    if code_verifier:
        flow.code_verifier = code_verifier
    return flow


def auth_url(session: Session) -> str:
    """Build the consent URL, persisting the PKCE verifier and CSRF state.

    The verifier generated here must survive until the callback lands, which is
    a separate HTTP request against a fresh Flow object. Storing it is what
    fixes "invalid_grant: Missing code verifier".
    """
    import secrets

    verifier = secrets.token_urlsafe(64)[:128]  # RFC 7636: 43-128 chars
    flow = _flow(session, code_verifier=verifier)

    url, state = flow.authorization_url(
        access_type="offline",       # we need a refresh token
        include_granted_scopes="true",
        prompt="consent",            # force a refresh token even on re-consent
    )
    set_setting(session, VERIFIER_KEY, verifier)
    set_setting(session, STATE_KEY, state or "")
    return url


def exchange_code(session: Session, code: str, state: str | None = None) -> None:
    expected_state = get_setting(session, STATE_KEY, "")
    if expected_state and state and state != expected_state:
        raise DriveError(
            "The authorisation response did not match this request. "
            "Start the connection again from Settings."
        )

    verifier = get_setting(session, VERIFIER_KEY, "")
    if not verifier:
        raise DriveError(
            "This authorisation has expired or was started elsewhere. "
            "Click Connect Google Drive again."
        )

    flow = _flow(session, state=expected_state or None, code_verifier=verifier)
    try:
        # requests-oauthlib sends client_id, client_secret, code,
        # grant_type=authorization_code, redirect_uri and code_verifier.
        flow.fetch_token(code=code)
    except Exception as exc:
        raise DriveError(f"Google rejected the authorisation: {exc}") from exc
    finally:
        # Single use, whatever the outcome.
        set_setting(session, VERIFIER_KEY, "")
        set_setting(session, STATE_KEY, "")

    creds = flow.credentials
    if not creds.refresh_token:
        raise DriveError(
            "Google did not return a refresh token. Remove this app's access at "
            "myaccount.google.com/permissions and connect again."
        )
    set_setting(session, TOKEN_KEY, creds.to_json())


def disconnect(session: Session) -> None:
    set_setting(session, TOKEN_KEY, "")
    set_setting(session, FOLDER_KEY, "")


def _credentials(session: Session) -> Credentials:
    raw = get_setting(session, TOKEN_KEY, "")
    if not raw:
        raise DriveError("Google Drive is not connected. Connect it in Settings.")
    try:
        creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    except Exception as exc:
        raise DriveError(f"Stored Drive credentials are unreadable: {exc}") from exc

    if not creds.valid:
        if not creds.refresh_token:
            raise DriveError("Drive access expired. Reconnect it in Settings.")
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise DriveError(f"Could not refresh Drive access: {exc}") from exc
        set_setting(session, TOKEN_KEY, creds.to_json())
    return creds


def _service(session: Session):
    return build("drive", "v3", credentials=_credentials(session), cache_discovery=False)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def _folder_id(session: Session, service) -> str:
    """One tidy folder for everything this app uploads."""
    cached = get_setting(session, FOLDER_KEY, "")
    if cached:
        try:
            service.files().get(fileId=cached, fields="id,trashed").execute()
            return cached
        except HttpError:
            pass  # deleted upstream; make a new one

    found = service.files().list(
        q=f"name='{FOLDER_NAME}' and mimeType='{FOLDER_MIME}' and trashed=false",
        spaces="drive", fields="files(id)", pageSize=1,
    ).execute().get("files", [])

    folder_id = found[0]["id"] if found else service.files().create(
        body={"name": FOLDER_NAME, "mimeType": FOLDER_MIME}, fields="id"
    ).execute()["id"]

    set_setting(session, FOLDER_KEY, folder_id)
    return folder_id


def upload(session: Session, path: Path, *, mimetype: str, name: str | None = None) -> dict[str, str]:
    """Upload a file, make it link-readable, and return its links."""
    if not path.exists():
        raise DriveError(f"{path.name} is not on disk any more.")

    service = _service(session)
    folder = _folder_id(session, service)

    try:
        created = service.files().create(
            body={"name": name or path.name, "parents": [folder]},
            media_body=MediaFileUpload(str(path), mimetype=mimetype, resumable=False),
            fields="id,name,webViewLink",
        ).execute()

        # Anyone with the link can read - required for channel subscribers.
        service.permissions().create(
            fileId=created["id"], body={"role": "reader", "type": "anyone"},
        ).execute()

        info = service.files().get(
            fileId=created["id"], fields="id,name,webViewLink,webContentLink"
        ).execute()
    except HttpError as exc:
        raise DriveError(f"Drive upload failed: {exc}") from exc

    log.info("Uploaded %s to Drive (%s)", info.get("name"), info.get("id"))
    return {
        "id": info["id"],
        "name": info.get("name", ""),
        "link": info.get("webViewLink", ""),
        "download": info.get("webContentLink", ""),
    }


def account_email(session: Session) -> str:
    """Which Google account is connected, for display in Settings."""
    try:
        service = build("oauth2", "v2", credentials=_credentials(session),
                        cache_discovery=False)
        return service.userinfo().get().execute().get("email", "")
    except Exception:
        return ""


def status(session: Session) -> dict[str, Any]:
    """Never raises - Settings must stay renderable."""
    configured = is_configured(session)
    connected = is_connected(session)
    detail = "Ready" if connected else (
        "Client saved — click Connect" if configured else "Not set up"
    )
    return {
        "configured": configured,
        "connected": connected,
        "detail": detail,
        "redirect_uri": redirect_uri(),
        "folder": FOLDER_NAME,
    }


# ---------------------------------------------------------------------------
# Provider dispatch
#
# Two ways to reach Drive:
#   mcp   - the claude.ai Google Drive connector, via the Claude Code CLI.
#           Nothing to set up; costs Max-plan usage per upload.
#   api   - a Google OAuth client you create once. Free and instant after that.
# ---------------------------------------------------------------------------

PROVIDER_KEY = "drive_provider"
PROVIDER_MCP = "mcp"
PROVIDER_API = "api"
PROVIDER_OFF = "off"


def get_provider(session: Session) -> str:
    return get_setting(session, PROVIDER_KEY, PROVIDER_MCP) or PROVIDER_MCP


def set_provider(session: Session, value: str) -> None:
    if value not in (PROVIDER_MCP, PROVIDER_API, PROVIDER_OFF):
        raise DriveError(f"Unknown Drive provider '{value}'.")
    set_setting(session, PROVIDER_KEY, value)


async def upload_asset(
    session: Session, path: Path, *, mimetype: str, name: str | None = None
) -> dict[str, str]:
    """Upload via whichever provider is selected."""
    from . import drive_mcp

    provider = get_provider(session)
    if provider == PROVIDER_OFF:
        raise DriveError("Drive uploads are turned off in Settings.")

    if provider == PROVIDER_MCP:
        try:
            return await drive_mcp.upload(path, mimetype=mimetype, name=name)
        except drive_mcp.TooLargeForMCP as exc:
            # The connector cannot carry documents of a realistic size. If the
            # direct client is set up, quietly use it rather than losing the link.
            if is_connected(session):
                log.info("File too large for the MCP connector; using the Drive API.")
                import asyncio

                return await asyncio.to_thread(
                    upload, session, path, mimetype=mimetype, name=name
                )
            raise DriveError(str(exc)) from exc
        except drive_mcp.DriveMCPError as exc:
            raise DriveError(str(exc)) from exc

    # Direct API - blocking client, so keep it off the event loop.
    import asyncio

    return await asyncio.to_thread(
        upload, session, path, mimetype=mimetype, name=name
    )


async def combined_status(session: Session) -> dict[str, Any]:
    """What Settings shows: the chosen provider plus both providers' health."""
    from . import drive_mcp

    api_status = status(session)
    mcp_status = await drive_mcp.check()
    provider = get_provider(session)

    if provider == PROVIDER_OFF:
        ready, detail = False, "Drive uploads are off."
    elif provider == PROVIDER_MCP:
        ready, detail = mcp_status["available"], mcp_status["detail"]
    else:
        ready, detail = api_status["connected"], api_status["detail"]

    return {
        "provider": provider,
        "ready": ready,
        "detail": detail,
        "folder": FOLDER_NAME,
        "mcp": mcp_status,
        "api": api_status,
    }
