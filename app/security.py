"""Optional bearer-token gate.

HC-6: single user, no signup/login. The token is OFF unless APP_BEARER_TOKEN is
set in .env, in which case /api/* requires `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .config import config


async def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = config.bearer_token
    if not expected:
        return  # disabled by default

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )
    supplied = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token.",
        )
