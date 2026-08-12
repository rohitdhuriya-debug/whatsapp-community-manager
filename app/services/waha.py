"""WAHA REST client.

Talks to the local WAHA container on 127.0.0.1:3000 (HC-3). Every request
carries the X-Api-Key header.

Multi-device: WAHA 2026.7.x runs several sessions in one container, so each
linked phone is just another session name. Every call here takes an explicit
session so nothing is accidentally sent from the wrong account.

Endpoint shapes are checked against the running container by
`scripts/verify_waha_api.py` (FR-4) rather than trusted from memory.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import config

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=5.0)

# Statuses WAHA reports for a session.
STATUS_WORKING = "WORKING"
STATUS_SCAN_QR = "SCAN_QR_CODE"
STATUS_STARTING = "STARTING"
STATUS_FAILED = "FAILED"
STATUS_STOPPED = "STOPPED"


class WahaError(RuntimeError):
    """Any failure talking to WAHA. Carries a human-readable hint for the UI."""

    def __init__(self, message: str, *, status_code: int | None = None, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.hint = hint

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.message, "status_code": self.status_code, "hint": self.hint}


def _headers() -> dict[str, str]:
    return {"X-Api-Key": config.waha_api_key, "Content-Type": "application/json"}


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    url = f"{config.waha_base_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(method, url, headers=_headers(), **kwargs)
    except httpx.ConnectError as exc:
        raise WahaError(
            "Cannot reach WAHA on 127.0.0.1:3000.",
            hint="Is Docker Desktop running? Try: docker compose up -d",
        ) from exc
    except httpx.TimeoutException as exc:
        raise WahaError(
            "WAHA did not respond in time.",
            hint="The container may still be starting. Check: docker compose logs waha",
        ) from exc

    if resp.status_code in (401, 403):
        raise WahaError(
            "WAHA rejected the API key.",
            status_code=resp.status_code,
            hint="WAHA_API_KEY in .env must match the value the container started with. "
            "After changing it run: docker compose up -d --force-recreate",
        )
    if resp.status_code == 404:
        raise WahaError(
            f"WAHA has no endpoint {path} (404).",
            status_code=404,
            hint="Your WAHA version may use a different route. Check http://localhost:3000",
        )
    if resp.status_code >= 400:
        raise WahaError(
            f"WAHA returned {resp.status_code}: {_short_body(resp)}",
            status_code=resp.status_code,
        )

    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _short_body(resp: httpx.Response, limit: int = 300) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            for key in ("message", "error", "detail"):
                if key in data:
                    return str(data[key])[:limit]
        return str(data)[:limit]
    except ValueError:
        return resp.text[:limit]


# ---------------------------------------------------------------------------
# Sessions (one per linked device)
# ---------------------------------------------------------------------------


async def list_sessions() -> list[dict[str, Any]]:
    """All sessions, including stopped ones."""
    data = await _request("GET", "/api/sessions", params={"all": "true"})
    return data if isinstance(data, list) else []


async def session_status(session_name: str | None = None) -> dict[str, Any]:
    """Live status for one session. Never raises - the UI must stay renderable."""
    name = session_name or config.waha_session
    try:
        data = await _request("GET", f"/api/sessions/{name}")
    except WahaError as exc:
        if exc.status_code == 404:
            return {
                "ok": False,
                "reachable": True,
                "name": name,
                "status": "NOT_CREATED",
                "error": f"No WAHA session named '{name}' yet.",
                "hint": "Add the device on the Devices tab and scan its QR.",
            }
        return {
            "ok": False,
            "reachable": exc.status_code is not None,
            "name": name,
            "status": "UNREACHABLE" if exc.status_code is None else "ERROR",
            "error": exc.message,
            "hint": exc.hint,
        }

    status = ""
    me: dict[str, Any] = {}
    if isinstance(data, dict):
        status = str(data.get("status") or data.get("state") or "").upper()
        me = data.get("me") or {}

    return {
        "ok": status == STATUS_WORKING,
        "reachable": True,
        "name": name,
        "status": status or "UNKNOWN",
        "phone": str(me.get("id", "")).split("@")[0] if me.get("id") else "",
        "push_name": me.get("pushName") or "",
        "error": None if status == STATUS_WORKING else f"Session status is {status or 'UNKNOWN'}.",
        "hint": ""
        if status == STATUS_WORKING
        else "Scan the QR on the Devices tab. Turn any VPN off while pairing.",
    }


async def create_session(name: str, start: bool = True) -> Any:
    return await _request("POST", "/api/sessions", json={"name": name, "start": start})


async def start_session(name: str | None = None) -> Any:
    session_name = name or config.waha_session
    return await _request("POST", f"/api/sessions/{session_name}/start")


async def stop_session(name: str) -> Any:
    return await _request("POST", f"/api/sessions/{name}/stop")


async def restart_session(name: str) -> Any:
    return await _request("POST", f"/api/sessions/{name}/restart")


async def delete_session(name: str) -> Any:
    return await _request("DELETE", f"/api/sessions/{name}")


async def logout_session(name: str) -> Any:
    return await _request("POST", f"/api/sessions/{name}/logout")


async def get_qr(name: str) -> bytes:
    """Raw PNG bytes of the pairing QR. Only valid while status=SCAN_QR_CODE."""
    url = f"{config.waha_base_url}/api/{name}/auth/qr"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                url, headers={"X-Api-Key": config.waha_api_key}, params={"format": "image"}
            )
    except httpx.HTTPError as exc:
        raise WahaError(f"Could not fetch the QR: {exc}") from exc

    if resp.status_code >= 400:
        raise WahaError(
            f"QR not available ({resp.status_code}).",
            status_code=resp.status_code,
            hint="The session must be in SCAN_QR_CODE state. Try restarting the device.",
        )
    return resp.content


# ---------------------------------------------------------------------------
# Chat discovery
# ---------------------------------------------------------------------------


def _pick(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _unwrap(data: Any) -> list[Any]:
    """WAHA's shape varies by engine and endpoint.

    NOWEB returns groups as an object keyed by chat id:
        {"1203...@g.us": {...}, "1203...@g.us": {...}}
    Other endpoints return a bare list, or {"data": [...]}. Handle all three.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            return data["data"]
        values = [v for v in data.values() if isinstance(v, dict)]
        if values:
            return values
    return []


def _normalise_chat(item: Any, fallback_id: str | None = None) -> dict[str, Any] | None:
    """Flatten a WAHA chat record to the shape the picker needs."""
    if isinstance(item, str):
        return {"id": item, "name": item, "participants": None,
                "is_community": False, "is_announce": False}
    if not isinstance(item, dict):
        return None

    raw_id = _pick(item, "id", "chatId", "jid") or fallback_id
    if isinstance(raw_id, dict):
        raw_id = _pick(raw_id, "_serialized", "id", "user")
    if not raw_id:
        return None

    # NOWEB puts the group name in `subject`, not `name`.
    name = _pick(item, "subject", "name", "title", "pushName") or str(raw_id)

    participants = item.get("participants")
    if isinstance(participants, list) and participants:
        count = len(participants)
    elif isinstance(item.get("size"), int):
        count = item["size"]
    else:
        count = None

    return {
        "id": str(raw_id),
        "name": str(name).strip(),
        "participants": count,
        "is_community": bool(item.get("isCommunity")),
        # The announcement group is the one you post to for a whole community.
        "is_announce": bool(item.get("isCommunityAnnounce") or item.get("announce")),
    }


def _records(data: Any) -> list[dict[str, Any] | None]:
    if isinstance(data, dict) and "data" not in data:
        pairs = [(k, v) for k, v in data.items() if isinstance(v, dict)]
        return [_normalise_chat(v, fallback_id=k) for k, v in pairs]
    return [_normalise_chat(i) for i in _unwrap(data)]


async def list_groups(session_name: str | None = None) -> list[dict[str, Any]]:
    """Groups, announcement groups included. chat_id ends @g.us."""
    name = session_name or config.waha_session
    try:
        data = await _request("GET", f"/api/{name}/groups", params={"limit": 1000})
    except WahaError as exc:
        if exc.status_code != 404:
            raise
        data = await _request("GET", f"/api/{name}/chats", params={"limit": 1000})

    out = [c for c in _records(data) if c and c["id"].endswith("@g.us")]
    # Announcement groups first - those are what you broadcast to - then by name.
    return sorted(out, key=lambda c: (not c["is_announce"], c["name"].lower()))


async def list_channels(session_name: str | None = None) -> list[dict[str, Any]]:
    """WhatsApp channels. chat_id ends @newsletter."""
    name = session_name or config.waha_session
    try:
        data = await _request("GET", f"/api/{name}/channels")
    except WahaError as exc:
        if exc.status_code != 404:
            raise
        return []

    out = [c for c in _records(data) if c and c["id"].endswith("@newsletter")]
    return sorted(out, key=lambda c: c["name"].lower())


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


async def send_text(
    chat_id: str, text: str, link_preview: bool = True, session_name: str | None = None
) -> dict[str, Any]:
    payload = {
        "session": session_name or config.waha_session,
        "chatId": chat_id,
        "text": text,
        "linkPreview": link_preview,
    }
    result = await _request("POST", "/api/sendText", json=payload)
    return result if isinstance(result, dict) else {"result": result}


async def send_poll(
    chat_id: str,
    question: str,
    options: list[str],
    multiple_answers: bool = False,
    session_name: str | None = None,
) -> dict[str, Any]:
    payload = {
        "session": session_name or config.waha_session,
        "chatId": chat_id,
        "poll": {
            "name": question,
            "options": options,
            "multipleAnswers": multiple_answers,
        },
    }
    result = await _request("POST", "/api/sendPoll", json=payload)
    return result if isinstance(result, dict) else {"result": result}


async def send_image(
    chat_id: str,
    *,
    filename: str,
    mimetype: str,
    data_b64: str,
    caption: str = "",
    session_name: str | None = None,
) -> dict[str, Any]:
    """Send an image with a caption - the format channels display best."""
    payload = {
        "session": session_name or config.waha_session,
        "chatId": chat_id,
        "file": {"mimetype": mimetype, "filename": filename, "data": data_b64},
    }
    if caption:
        payload["caption"] = caption
    result = await _request("POST", "/api/sendImage", json=payload)
    return result if isinstance(result, dict) else {"result": result}


async def send_file(
    chat_id: str,
    *,
    filename: str,
    mimetype: str,
    data_b64: str,
    caption: str = "",
    session_name: str | None = None,
) -> dict[str, Any]:
    """Send a generated PDF / spreadsheet as a document."""
    payload = {
        "session": session_name or config.waha_session,
        "chatId": chat_id,
        "file": {"mimetype": mimetype, "filename": filename, "data": data_b64},
    }
    if caption:
        payload["caption"] = caption
    result = await _request("POST", "/api/sendFile", json=payload)
    return result if isinstance(result, dict) else {"result": result}
