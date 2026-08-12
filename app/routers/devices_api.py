"""Linked WhatsApp accounts.

Each device is one WAHA session. WAHA 2026.7.x runs several concurrently, so
extra phones cost nothing.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from ..config import config
from ..db import get_session
from ..models import Device, Target
from ..services import waha

router = APIRouter(prefix="/api/devices", tags=["devices"])


class DeviceIn(BaseModel):
    name: str
    session_name: str | None = None  # derived from the name when omitted


class DevicePatch(BaseModel):
    name: str | None = None
    is_primary: bool | None = None


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:24] or "device"


def _unique_session_name(session: Session, base: str) -> str:
    existing = {d.session_name for d in session.exec(select(Device)).all()}
    if base not in existing:
        return base
    for n in range(2, 50):
        candidate = f"{base}-{n}"
        if candidate not in existing:
            return candidate
    raise HTTPException(status_code=409, detail="Too many devices with similar names.")


def _get_or_404(session: Session, device_id: int) -> Device:
    device = session.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    return device


def ensure_primary_device(session: Session) -> Device:
    """Adopt the pre-existing `default` session as a device on first run.

    Everything built before multi-device used a single session; this makes that
    account show up as a normal device rather than vanishing.
    """
    device = session.exec(select(Device).where(Device.is_primary)).first()
    if device is None:
        device = session.exec(
            select(Device).where(Device.session_name == config.waha_session)
        ).first()
        if device is None:
            device = Device(
                name="My WhatsApp", session_name=config.waha_session, is_primary=True
            )
        else:
            device.is_primary = True
        session.add(device)
        session.commit()
        session.refresh(device)

    # Targets created before multi-device have no device. They all belong to
    # the original session, so adopt them rather than leaving them unroutable.
    # This runs on every call, not just first setup - a target added by an
    # older code path would otherwise stay orphaned forever.
    orphans = session.exec(select(Target).where(Target.device_id.is_(None))).all()
    if orphans:
        for target in orphans:
            target.device_id = device.id
            session.add(target)
        session.commit()

    return device


@router.get("/waha/health")
async def waha_health(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Everything about the WAHA container, so nothing needs its own UI.

    Also surfaces sessions that exist in WAHA but not in this app (left over
    from a failed link), which are otherwise invisible and confusing.
    """
    try:
        sessions = await waha.list_sessions()
    except waha.WahaError as exc:
        return {
            "reachable": False,
            "error": exc.message,
            "hint": exc.hint or "Start it with: docker compose up -d",
            "base_url": config.waha_base_url,
            "sessions": [],
            "orphans": [],
        }

    known = {d.session_name for d in session.exec(select(Device)).all()}
    rows = [
        {
            "name": s.get("name"),
            "status": s.get("status"),
            "linked": s.get("name") in known,
            "me": (s.get("me") or {}).get("id", ""),
        }
        for s in sessions
        if isinstance(s, dict)
    ]
    return {
        "reachable": True,
        "error": None,
        "hint": "",
        "base_url": config.waha_base_url,
        "sessions": rows,
        "orphans": [r["name"] for r in rows if not r["linked"]],
    }


@router.delete("/waha/sessions/{name}")
async def delete_orphan_session(name: str, session: Session = Depends(get_session)) -> dict:
    """Remove a WAHA session that has no device record."""
    if session.exec(select(Device).where(Device.session_name == name)).first():
        raise HTTPException(
            status_code=409,
            detail="That session belongs to a device. Remove the device instead.",
        )
    try:
        await waha.delete_session(name)
    except waha.WahaError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    return {"ok": True}


@router.get("")
async def list_devices(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Every device with its live WAHA status folded in."""
    ensure_primary_device(session)
    devices = session.exec(select(Device).order_by(Device.id)).all()

    counts: dict[int, int] = {}
    for target in session.exec(select(Target)).all():
        counts[target.device_id or 0] = counts.get(target.device_id or 0, 0) + 1

    out = []
    for device in devices:
        status = await waha.session_status(device.session_name)

        # Keep the cached identity fresh once a device pairs.
        if status.get("ok") and (status.get("phone") or status.get("push_name")):
            changed = False
            if status["phone"] and device.phone != status["phone"]:
                device.phone, changed = status["phone"], True
            if status["push_name"] and device.push_name != status["push_name"]:
                device.push_name, changed = status["push_name"], True
            if changed:
                session.add(device)
                session.commit()

        out.append(
            {
                "id": device.id,
                "name": device.name,
                "session_name": device.session_name,
                "is_primary": device.is_primary,
                "phone": device.phone,
                "push_name": device.push_name,
                "target_count": counts.get(device.id, 0),
                "status": status["status"],
                "ok": status["ok"],
                "error": status["error"],
                "hint": status["hint"],
            }
        )
    return out


@router.post("", status_code=201)
async def create_device(
    payload: DeviceIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the device a name.")

    session_name = _unique_session_name(
        session, _slug(payload.session_name or name)
    )

    try:
        await waha.create_session(session_name, start=True)
    except waha.WahaError as exc:
        raise HTTPException(
            status_code=502, detail=f"{exc.message} {exc.hint}".strip()
        ) from exc

    device = Device(name=name, session_name=session_name)
    session.add(device)
    session.commit()
    session.refresh(device)
    return {"id": device.id, "name": device.name, "session_name": device.session_name}


@router.patch("/{device_id}")
def update_device(
    device_id: int, payload: DevicePatch, session: Session = Depends(get_session)
) -> dict[str, Any]:
    device = _get_or_404(session, device_id)
    if payload.name is not None:
        device.name = payload.name.strip() or device.name
    if payload.is_primary:
        for other in session.exec(select(Device)).all():
            other.is_primary = other.id == device_id
            session.add(other)
    session.add(device)
    session.commit()
    session.refresh(device)
    return {"id": device.id, "name": device.name, "is_primary": device.is_primary}


@router.get("/{device_id}/qr")
async def device_qr(device_id: int, session: Session = Depends(get_session)) -> Response:
    """PNG pairing QR, self-healing.

    WAHA/Baileys issues a fixed number of QR refreshes (roughly two minutes'
    worth) and then force-stops the session with "QR refs attempts ended".
    Every later request then fails, which reads as "couldn't link device" on
    the phone. So if the session is not offering a QR, restart it and wait for
    a fresh one instead of returning an error.
    """
    import asyncio

    device = _get_or_404(session, device_id)
    state = await waha.session_status(device.session_name)

    if state["status"] != waha.STATUS_SCAN_QR:
        if state["status"] == waha.STATUS_WORKING:
            raise HTTPException(status_code=409, detail="This device is already linked.")

        try:
            if state["status"] == "NOT_CREATED":
                await waha.create_session(device.session_name, start=True)
            else:
                await waha.restart_session(device.session_name)
        except waha.WahaError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc

        # Reaching SCAN_QR_CODE took ~3s in testing; 20s is a generous ceiling.
        for _ in range(20):
            await asyncio.sleep(1)
            state = await waha.session_status(device.session_name)
            if state["status"] in (waha.STATUS_SCAN_QR, waha.STATUS_WORKING):
                break

        if state["status"] == waha.STATUS_WORKING:
            raise HTTPException(status_code=409, detail="This device is already linked.")
        if state["status"] != waha.STATUS_SCAN_QR:
            raise HTTPException(
                status_code=409,
                detail=f"Session is {state['status']} and did not produce a QR. "
                "Press Restart and try again.",
            )

    try:
        png = await waha.get_qr(device.session_name)
    except waha.WahaError as exc:
        raise HTTPException(
            status_code=409, detail=f"{exc.message} {exc.hint}".strip()
        ) from exc
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/{device_id}/restart")
async def restart_device(device_id: int, session: Session = Depends(get_session)) -> dict:
    device = _get_or_404(session, device_id)
    try:
        await waha.restart_session(device.session_name)
    except waha.WahaError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    return {"ok": True}


@router.post("/{device_id}/logout")
async def logout_device(device_id: int, session: Session = Depends(get_session)) -> dict:
    """Unlink the phone but keep the device and its targets."""
    device = _get_or_404(session, device_id)
    try:
        await waha.logout_session(device.session_name)
    except waha.WahaError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    device.phone = ""
    device.push_name = ""
    session.add(device)
    session.commit()
    return {"ok": True}


@router.delete("/{device_id}", status_code=204)
async def delete_device(device_id: int, session: Session = Depends(get_session)) -> None:
    device = _get_or_404(session, device_id)

    in_use = session.exec(select(Target).where(Target.device_id == device_id)).all()
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"{len(in_use)} target(s) still use this device. Delete them first: "
            + ", ".join(t.name for t in in_use[:5]),
        )

    try:
        await waha.delete_session(device.session_name)
    except waha.WahaError:
        # The WAHA session may already be gone; removing our record is still right.
        pass

    session.delete(device)
    session.commit()
