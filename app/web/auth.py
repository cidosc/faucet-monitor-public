"""Dashboard authentication: HTTP Basic with a bcrypt hash.

The Compose deployment publishes the dashboard on loopback only. Use an SSH
tunnel or an authenticated HTTPS reverse proxy for remote access; HTTP Basic
authentication alone does not encrypt traffic.
"""

from __future__ import annotations

import secrets

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

_basic = HTTPBasic(auto_error=False)
_cached_hash: bytes | None = None


def _password_hash() -> bytes | None:
    """Return the bcrypt hash to check against, hashing a plaintext DASHBOARD_PASSWORD once."""
    global _cached_hash
    if _cached_hash is not None:
        return _cached_hash

    if settings.dashboard_password_hash:
        _cached_hash = settings.dashboard_password_hash.encode()
        return _cached_hash

    if settings.dashboard_password:
        _cached_hash = bcrypt.hashpw(settings.dashboard_password.encode(), bcrypt.gensalt())
        log.info("dashboard_password_hashed_at_startup")
        return _cached_hash

    log.warning("dashboard_no_password_configured")
    return None


def auth_disabled() -> bool:
    return not settings.dashboard_password_hash and not settings.dashboard_password


async def require_auth(
    credentials: HTTPBasicCredentials | None = Depends(_basic),  # noqa: B008 - FastAPI dependency
) -> str:
    stored = _password_hash()
    if stored is None:
        # Fail closed: if no password is configured we refuse rather than expose data.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dashboard password not configured (set DASHBOARD_PASSWORD)",
        )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    user_ok = secrets.compare_digest(credentials.username, settings.dashboard_username)
    try:
        pass_ok = bcrypt.checkpw(credentials.password.encode(), stored)
    except ValueError:
        pass_ok = False

    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

