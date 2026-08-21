"""Single-operator authentication (§66).

This deployment has one operator, not a user table — ``AUTH_USERNAME`` /
``AUTH_PASSWORD`` are literal credentials in configuration, not a hash to
look up. That is why login compares them directly (in constant time, via
``hmac.compare_digest``, so a timing side-channel can't leak the password a
character at a time) rather than involving ``argon2-cffi``: hashing a
password that already lives in plaintext in the environment would be
theatre, not security. If this ever grows into multi-user auth, that is the
point to introduce a real user store and password hashing — not before.

The session itself is a JWT in an httpOnly cookie, signed with
``AUTH_SECRET``. Not a bearer token in a header: the frontend's
``credentials: 'include'`` (see ``src/api/client.js``) already assumes
cookie-based sessions, and httpOnly means a XSS bug in the React app cannot
read the token and exfiltrate it — the worst it can do is ride along on
requests to this API, same as any cookie-authenticated site.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone

import jwt
import structlog
from fastapi import HTTPException, Request

from app.config import Settings

log = structlog.get_logger(__name__)

COOKIE_NAME = "annie_session"
SESSION_LIFETIME = timedelta(days=30)
ALGORITHM = "HS256"


def verify_credentials(settings: Settings, username: str, password: str) -> bool:
    if not settings.auth_username or not settings.auth_password:
        return False
    user_ok = hmac.compare_digest(username.strip(), settings.auth_username)
    pass_ok = hmac.compare_digest(password, settings.auth_password)
    return user_ok and pass_ok


def create_session_token(settings: Settings, username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": username, "iat": now, "exp": now + SESSION_LIFETIME}
    return jwt.encode(payload, settings.auth_secret, algorithm=ALGORITHM)


def _verify_session_token(settings: Settings, token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.auth_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    return payload.get("sub")


async def require_auth(request: Request) -> str:
    """FastAPI dependency — raises 401 without a valid session cookie.

    Applied at the router level (see ``app/main.py``) rather than per-route,
    so a new route added later is protected by default instead of by
    remembering to add this.
    """
    from app.config import get_settings

    settings = get_settings()
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    username = _verify_session_token(settings, token)
    if username is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid.")
    return username
